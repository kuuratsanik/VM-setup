"""Two-stage promotion of a tuned model, each stage a PR: expose it as the 'tuned' alias, then make it the default after the eval gate passes."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

import config  # noqa: E402,F401
import evalgate  # noqa: E402
import pr  # noqa: E402
import yaml  # noqa: E402
from openai import OpenAI  # noqa: E402


def override_edit(**changes):
    def edit(repo):
        path = repo / "profile.override.yaml"
        doc = yaml.safe_load(path.read_text()) or {}
        ai = doc.setdefault("ai", {})
        for key, value in changes.items():
            if value is None:
                ai.pop(key, None)
            else:
                ai[key] = value
        return {"profile.override.yaml": yaml.safe_dump(doc, sort_keys=False)}

    return edit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["candidate", "default"])
    ap.add_argument("model", help="fine-tuned model id (ft:...) or the LoRA module name served by vLLM")
    args = ap.parse_args()

    if args.stage == "candidate":
        url = pr.open_pr("agent/tuned-candidate", override_edit(tuned_candidate=args.model), f"Expose tuned model {args.model} as 'tuned'", "Exposes the model behind the `tuned` alias for evaluation. It is not used for routing yet.")
    else:
        client = OpenAI(base_url=os.environ.get("LITELLM_URL", "http://127.0.0.1:4000"), api_key=os.environ.get("LITELLM_KEY", "none"))
        result = evalgate.compare(client, "default", "tuned")
        print(json.dumps(result, indent=2))
        if not result["pass"]:
            sys.exit("promote: eval gate failed; not proposing")
        url = pr.open_pr("agent/tuned-default", override_edit(tuned_model=args.model, tuned_candidate=None), f"Route default to tuned model {args.model}", "Eval gate passed:\n```json\n" + json.dumps(result, indent=2) + "\n```")
    print(f"promote: {url or 'proposal already up to date'}")


if __name__ == "__main__":
    main()
