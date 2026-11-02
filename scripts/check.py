"""Run project tests and check that committed dashboards match the generator."""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT)
    if result.returncode:
        return result.returncode
    spec = importlib.util.spec_from_file_location("dashboard_generator", ROOT / "scripts" / "generate_dashboards.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    with tempfile.TemporaryDirectory() as directory:
        generator.DESTINATION = Path(directory)
        generator.main()
        actual = ROOT / "grafana" / "dashboards"
        expected_files = {file.name for file in generator.DESTINATION.glob("*.json")}
        if expected_files != {file.name for file in actual.glob("*.json")}:
            raise ValueError("Dashboard mancanti o obsolete: esegui python3 scripts/generate_dashboards.py")
        for file in generator.DESTINATION.glob("*.json"):
            if json.loads(file.read_text()) != json.loads((actual / file.name).read_text()):
                raise ValueError(f"Dashboard non aggiornata: {file.name}; esegui il generatore")
    print("Test e dashboard generati verificati.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
