import os
import signal
import subprocess
import time


def test_entrypoint_stays_alive_after_starting_background_services(tmp_path):
    app = tmp_path / "app"
    fake_bin = tmp_path / "bin"
    log_path = tmp_path / "calls.log"
    app.mkdir()
    fake_bin.mkdir()

    python3 = fake_bin / "python3"
    python3.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> \"$ENTRYPOINT_TEST_LOG\"\n"
        "exit 0\n"
    )
    python3.chmod(0o755)

    ts = fake_bin / "ts"
    ts.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n")
    ts.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "APP_DIR": str(app),
            "ENTRYPOINT_TEST_LOG": str(log_path),
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        }
    )

    proc = subprocess.Popen(
        ["bash", "entrypoint.sh"],
        cwd=os.getcwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        deadline = time.time() + 2
        while time.time() < deadline:
            if log_path.exists():
                logged = log_path.read_text()
                if "main.py" in logged and "-m lib.generate_tibber_visual" in logged:
                    break
            time.sleep(0.05)

        assert proc.poll() is None
        logged = log_path.read_text()
        assert "main.py" in logged
        assert "-m lib.generate_tibber_visual" in logged
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.communicate(timeout=5)
