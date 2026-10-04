"""Compute facade used by both the chat tools and the REST API; clients exist only when their keys are configured."""
import os
import re

from jarvis.compute.kaggle import Kaggle
from jarvis.compute.runpod import RunPod

NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")


class NotConfigured(Exception):
    pass


class Compute:
    def __init__(self, env=None, runpod_transport=None, kaggle_runner=None):
        self.env, self.runpod_transport, self.kaggle_runner = env if env is not None else os.environ, runpod_transport, kaggle_runner

    def runpod(self):
        key = self.env.get("RUNPOD_API_KEY")
        if not key:
            raise NotConfigured("RunPod is not set up; add its API key in the Setup tab")
        return RunPod(key, float(self.env.get("JARVIS_RUNPOD_MAX_HOURLY_USD", "1.0")), float(self.env.get("JARVIS_RUNPOD_MAX_HOURS", "4")), self.runpod_transport)

    def kaggle(self):
        token, user = self.env.get("KAGGLE_API_TOKEN"), self.env.get("KAGGLE_USERNAME")
        if not token or not user:
            raise NotConfigured("Kaggle is not set up; add its token and username in the Setup tab")
        return Kaggle(token, user, self.kaggle_runner)

    async def run(self, name, args):
        if name == "runpod_list_pods":
            return await self.runpod().list_pods()
        if name == "runpod_create_pod":
            if not NAME_RE.match(str(args.get("name", ""))):
                raise ValueError("name must be lowercase letters, digits and dashes")
            return await self.runpod().create_pod(args["name"], args["gpu_type"], hours=float(args.get("hours", 2)))
        if name == "kaggle_status":
            return await self.kaggle().status(str(args.get("ref", "")))
        if name == "kaggle_run_script":
            ref = await self.kaggle().push_script(str(args.get("slug", "")), str(args.get("code", "")), gpu=bool(args.get("gpu", True)))
            return {"ref": ref, "started": True}
        raise ValueError(f"unknown compute action {name}")
