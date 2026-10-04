"""Human feedback on Operator incidents: it steers retrieval and selects what the Librarian and trainer may learn from."""
import argparse
import sys

import memory


def main():
    if not memory.enabled():
        sys.exit("feedback: VMSETUP_DATABASE_URL is not set (vector memory disabled)")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="recent incidents")
    ls.add_argument("-n", type=int, default=20)
    mark = sub.add_parser("mark", help="label an incident")
    mark.add_argument("id", type=int)
    mark.add_argument("label", choices=["good", "bad"])
    mark.add_argument("note", nargs="?")
    args = ap.parse_args()

    if args.cmd == "list":
        for row in memory.recent(args.n):
            print(*row, sep="\t")
    elif memory.set_feedback(args.id, args.label, args.note) == 0:
        sys.exit(f"feedback: no incident {args.id}")
    else:
        print(f"incident {args.id} marked {args.label}")


if __name__ == "__main__":
    main()
