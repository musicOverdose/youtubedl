import os
import subprocess
import yaml
import pytest


def test_compose_port_mapping_default():
    """Verify docker-compose default port mapping: 8087:8080"""
    env = os.environ.copy()
    env.pop("WEB_HOST_PORT", None)

    res = subprocess.run(
        ["docker", "compose", "config"],
        cwd="/home/farzad/youtubedl",
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"docker compose config failed: {res.stderr}"

    config = yaml.safe_load(res.stdout)
    web_ports = config["services"]["web"]["ports"]
    assert any(
        (isinstance(p, dict) and str(p.get("published")) == "8087" and p.get("target") == 8080)
        or (isinstance(p, str) and "8087:8080" in p)
        for p in web_ports
    ), f"Expected 8087:8080 in web ports, got {web_ports}"


def test_compose_port_mapping_custom_override():
    """Verify docker-compose custom port override: WEB_HOST_PORT=8090 -> 8090:8080"""
    env = os.environ.copy()
    env["WEB_HOST_PORT"] = "8090"

    res = subprocess.run(
        ["docker", "compose", "config"],
        cwd="/home/farzad/youtubedl",
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"docker compose config failed: {res.stderr}"

    config = yaml.safe_load(res.stdout)
    web_ports = config["services"]["web"]["ports"]
    assert any(
        (isinstance(p, dict) and str(p.get("published")) == "8090" and p.get("target") == 8080)
        or (isinstance(p, str) and "8090:8080" in p)
        for p in web_ports
    ), f"Expected 8090:8080 in web ports with WEB_HOST_PORT=8090, got {web_ports}"
