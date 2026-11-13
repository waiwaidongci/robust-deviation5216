import json,time,traceback
from pathlib import Path
from .config.database import engine,SessionLocal
from .config.schema_upgrade import ensure_schema
from .models import DeviationRun
from .constants.missing import resolve_missing_policy
from .constants.status import RunStatus
from .services.run_service import queue
from .services.analysis_service import analyze_deviation
from .services.audit_service import log
ensure_schema(engine);Path("/artifacts").mkdir(exist_ok=True)
while True:
    Path("/artifacts/heartbeat").write_text(str(time.time()))
    job=queue.blpop("deviation:jobs",timeout=2)
    if not job:continue
    with SessionLocal() as db:
        run=db.get(DeviationRun,int(job[1]))
        if not run or run.status!=RunStatus.QUEUED:continue
        run.status=RunStatus.RUNNING;db.commit()
        try:
            run.result=analyze_deviation(run.input_snapshot,run.alpha,run.seed,resolve_missing_policy(run.missing_policy));run.status=RunStatus.COMPLETED
            Path(f"/artifacts/run-{run.id}.json").write_text(json.dumps(run.result,ensure_ascii=False));log(db,"worker","deviation.completed",run.id)
        except Exception as e:run.status=RunStatus.FAILED;run.error=str(e);traceback.print_exc()
        db.commit()
