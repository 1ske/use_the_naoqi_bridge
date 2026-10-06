"""One run's agent container: network, proxy, login, start, stop, collect, remove (section 5.1.7).

Per run:
  * a new internal Docker network (no route to the internet or the host) named pilot-<run_id>;
  * an allowlisting proxy (naoqi-pilot/proxy) on that network and a normal one, with the agent's proxy_allow;
  * the bridge container attached to the network under the alias "bridge" (the MCP server's NAOQI_BRIDGE_URL);
  * a new container from the agent's image. Nothing from the host is mounted except the run's output directory
    (/out). The login comes from its template: the A5 token as an environment variable, read from the template
    file at the start of the run; the A6 token file copied in before start; the A1-A4 OpenRouter key (one per
    run, see step 5) as an environment variable. Secret values never appear in a command line or a log.
After the run the container, the proxy and the network are removed, and the bridge is detached.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENTS = json.loads((REPO / "pilot" / "config" / "agents.json").read_text())
TEMPLATES = Path.home() / ".naoqi-pilot" / "templates"
CLAUDE_TOKEN = TEMPLATES / "claude" / "oauth-token"
AGY_TOKEN = TEMPLATES / "agy" / "antigravity-cli" / "antigravity-oauth-token"
NO_PROXY = "bridge,localhost,127.0.0.1"


def docker(*args, env=None, check=True, capture=True):
    result = subprocess.run(["docker", *args], env=env, text=True,
                            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    if check and result.returncode != 0:
        raise RuntimeError("docker %s failed: %s" % (args[0], (result.stderr or "").strip()[:300]))
    return (result.stdout or "").strip()


def image_id(image):
    return docker("image", "inspect", "--format", "{{.Id}}", image)


class AgentContainer:
    def __init__(self, run_id, agent, out_dir, bridge_container=None, openrouter_key=None, test_env=None,
                 proxy_allow=None, image=None):
        """test_env / proxy_allow: only for free test runs against pilot/tools/fake_llm.py. test_env replaces the
        login and points the harness at the fake endpoint (NO_PROXY there is added to the default); proxy_allow
        replaces the agent's allowlist, so that nothing outside the run's network is reachable."""
        self.run_id, self.agent, self.spec = run_id, agent, AGENTS[agent]
        self.test_env = dict(test_env or {})
        self.proxy_allow = proxy_allow or self.spec["proxy_allow"]
        self.image = image or self.spec["image"]  # another image only for the instruction and image check
        self.out_dir = Path(out_dir).resolve()
        self.bridge = bridge_container
        self.openrouter_key = openrouter_key
        self.net = "pilot-%s" % run_id
        self.proxy = "pilot-%s-proxy" % run_id
        self.name = "pilot-%s-agent" % run_id
        self.created = []

    # --- setup ----------------------------------------------------------------------------------------------
    def setup_network(self):
        docker("network", "create", "--internal", self.net)
        self.created.append(("network", self.net))
        docker("run", "-d", "--name", self.proxy, "-e", "PROXY_ALLOW=" + " ".join(self.proxy_allow),
               "naoqi-pilot/proxy")
        self.created.append(("container", self.proxy))
        docker("network", "connect", self.net, self.proxy)
        if self.bridge:
            docker("network", "connect", "--alias", "bridge", self.net, self.bridge)
            self.created.append(("bridge", self.bridge))

    def create(self, prompt):
        """Create the agent container (not started) and stage its login. Returns the image ID used."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        proxy = "http://%s:8888" % self.proxy
        args = ["create", "--name", self.name, "--network", self.net, "--hostname", "agent",
                "--security-opt", "no-new-privileges", "-v", "%s:/out" % self.out_dir]
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            args += ["-e", "%s=%s" % (var, proxy)]
        test_env = {k: v for k, v in self.test_env.items() if k != "NO_PROXY"}
        no_proxy = ",".join(filter(None, [NO_PROXY, self.test_env.get("NO_PROXY")]))
        args += ["-e", "NO_PROXY=" + no_proxy, "-e", "no_proxy=" + no_proxy]
        env = dict(os.environ)
        harness = self.spec["harness"]
        if self.test_env:
            # Test run: the fake login and endpoint replace the template login, which is not read at all.
            env.update(test_env)
            for name in sorted(test_env):
                args += ["-e", name]
        elif harness == "claude-code":
            # `-e NAME` without a value: docker takes it from this process's environment, not the command line.
            env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_TOKEN.read_text().strip()
            args += ["-e", "CLAUDE_CODE_OAUTH_TOKEN"]
        elif harness == "opencode":
            if not self.openrouter_key:
                raise ValueError("%s needs the run's OpenRouter key" % self.agent)
            env["OPENROUTER_API_KEY"] = self.openrouter_key
            args += ["-e", "OPENROUTER_API_KEY"]
        args += [self.image, "pilot-agent", prompt]
        if harness == "opencode":
            args.append(self.spec["model"])
        docker(*args, env=env)
        self.created.append(("container", self.name))
        if harness == "agy" and not self.test_env:
            self.stage_agy_login(self.name)
        return docker("inspect", "--format", "{{.Image}}", self.name)

    @staticmethod
    def stage_agy_login(container):
        """Copy only the template's token file into the container (the entrypoint installs it)."""
        with tempfile.TemporaryDirectory() as stage:
            target = Path(stage) / ".gemini" / "antigravity-cli"
            target.mkdir(parents=True)
            (target / "antigravity-oauth-token").write_bytes(AGY_TOKEN.read_bytes())
            os.chmod(target / "antigravity-oauth-token", 0o600)
            docker("cp", stage + "/.", "%s:/stage" % container)

    # --- run --------------------------------------------------------------------------------------------------
    def start(self):
        docker("start", self.name)

    def running(self):
        return docker("inspect", "--format", "{{.State.Running}}", self.name, check=False) == "true"

    def exit_code(self):
        return docker("inspect", "--format", "{{.State.ExitCode}}", self.name, check=False)

    def stop(self, grace=5):
        docker("stop", "-t", str(grace), self.name, check=False)

    def collect(self):
        """Copy what the harness keeps inside the container into the run folder (agy's conversation logs)."""
        if self.spec["harness"] == "agy":
            docker("cp", "%s:/home/agent/.gemini/antigravity-cli/brain" % self.name, str(self.out_dir / "agy-brain"),
                   check=False)

    def remove(self):
        for kind, name in reversed(self.created):
            if kind == "container":
                docker("rm", "-f", name, check=False)
            elif kind == "bridge":
                docker("network", "disconnect", "-f", self.net, name, check=False)
            elif kind == "network":
                docker("network", "rm", name, check=False)
        self.created = []


def agy_login_ok(run_id):
    """Free pre-run check for A6: `agy models` (no model request) in a throwaway container with a copy of the
    template login, through A6's proxy allowlist. False means the login has expired: the run is invalid."""
    probe = AgentContainer(run_id + "-login", "A6", tempfile.mkdtemp(prefix="pilot-login-"))
    try:
        probe.setup_network()
        proxy = "http://%s:8888" % probe.proxy
        args = ["create", "--name", probe.name, "--network", probe.net, "--hostname", "agent"]
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            args += ["-e", "%s=%s" % (var, proxy)]
        docker(*args, "naoqi-pilot/agy", "agy", "models")
        probe.created.append(("container", probe.name))
        probe.stage_agy_login(probe.name)
        output = docker("start", "-a", probe.name, check=False)
        return probe.exit_code() == "0" and "gemini-3.8-flash-medium" in output
    finally:
        probe.remove()
