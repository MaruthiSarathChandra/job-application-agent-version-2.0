import argparse
import json
import sys
from pathlib import Path
from .intake import IntakeStore


def main():
    parser = argparse.ArgumentParser(description='Phase 1: import and qualify a captured job posting')
    parser.add_argument('--job-file', required=True)
    parser.add_argument('--requirements', required=True)
    parser.add_argument('--database', default='data/agent.db')
    args = parser.parse_args()
    store = None
    try:
        job = json.loads(Path(args.job_file).read_text(encoding='utf-8'))
        requirements = json.loads(Path(args.requirements).read_text(encoding='utf-8'))
        if not isinstance(job, dict) or not isinstance(requirements, dict):
            raise ValueError('Job and requirements must be JSON objects')
        allowed = {'title_keywords', 'excluded_title_keywords', 'locations'}
        if set(requirements) - allowed:
            raise ValueError('Unsupported requirement key; refusing to ignore a rule')
        if any(not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v)
               for v in requirements.values()):
            raise ValueError('Requirement values must be lists of nonempty strings')
        Path(args.database).parent.mkdir(parents=True, exist_ok=True)
        store = IntakeStore(args.database)
        print(json.dumps(store.import_job(job, requirements), indent=2))
    except (ValueError, OSError) as exc:
        print(f'Intake failed: {exc}', file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
