"""Start the sequential discovery scheduler and continuous resume worker together."""
import argparse
import os
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.error import URLError


def main():
    parser=argparse.ArgumentParser(description='Run Agent 1 dashboard and Agent 2 worker')
    parser.add_argument('--database',default='data/agent.db')
    parser.add_argument('--profile',default='data/profile.json')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--scan-every-minutes',type=int,help='Override persisted discovery cadence; 0 disables')
    parser.add_argument('--require-resume-review',action='store_true')
    args=parser.parse_args()
    children=[]
    try:
        command=[sys.executable,'-m','agent.dashboard','--database',args.database,'--profile',args.profile,'--port',str(args.port)]
        if args.scan_every_minutes is not None:command+=['--scan-every-minutes',str(args.scan_every_minutes)]
        dashboard=subprocess.Popen(command);children.append(dashboard)
        print('Dashboard starts now. Save a verified profile and a manual search to begin continuous processing.',flush=True)
        worker=None;last_error=None
        while dashboard.poll() is None:
            if worker is None:
                try:
                    # Wait for dashboard migrations/server startup before opening the worker DB.
                    with urlopen(f'http://127.0.0.1:{args.port}/api/state',timeout=2) as response:
                        if response.status!=200:
                            raise ValueError('Dashboard is not ready')
                    from .resume_worker import load_profile
                    if not os.getenv('OPENAI_API_KEY') or not os.getenv('OPENAI_MODEL'):
                        raise ValueError('Set OPENAI_API_KEY and OPENAI_MODEL in this shell before starting the launcher')
                    load_profile(args.profile)
                    command=[sys.executable,'-m','agent.resume_worker','--database',args.database,'--profile',args.profile,'--watch']
                    if args.require_resume_review:command+=['--require-resume-review']
                    worker=subprocess.Popen(command);children.append(worker)
                except (ValueError,OSError,URLError) as exc:
                    if str(exc)!=last_error:print('Agent 2 waiting: '+str(exc),flush=True);last_error=str(exc)
            elif worker.poll() is not None:
                print('Agent 2 exited. Inspect its error and restart the launcher after resolving it; dashboard remains available.',flush=True)
                while dashboard.poll() is None:time.sleep(1)
                break
            time.sleep(2)
    except KeyboardInterrupt:pass
    finally:
        for child in children:
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait()

if __name__=='__main__':main()
