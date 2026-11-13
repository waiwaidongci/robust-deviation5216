from fastapi import HTTPException
from ..constants.missing import MissingPolicy
from ..models import ArchiveStudy,ClimateWindow,FeatureSpec
from ..utils.serialize import record
from .analysis_service import prepare_snapshot,missing_cells

def get_study(db,sid):
    row=db.get(ArchiveStudy,sid)
    if not row:raise HTTPException(404,"研究不存在")
    return row
def create_snapshot(db,study):return {"study":record(study),"features":[record(f) for f in db.query(FeatureSpec).filter_by(study_id=study.id).order_by(FeatureSpec.position)],"windows":[record(w) for w in db.query(ClimateWindow).filter_by(study_id=study.id).order_by(ClimateWindow.code)]}
def list_studies(db):
    out=[]
    for s in db.query(ArchiveStudy).order_by(ArchiveStudy.code):
        snapshot=create_snapshot(db,s);supported=True;reason='拆分完整、训练满秩；可运行稳健研究';missing=missing_cells(snapshot)
        try:prepare_snapshot(snapshot,MissingPolicy.REJECT.value)
        except ValueError as reject_error:
            try:
                prepare_snapshot(snapshot,MissingPolicy.IMPUTE_TRAIN_MEDIAN.value)
                reason=f"{missing}个训练/校准/测试观测缺测；严格策略拒绝，可在创建时选择训练中位数补齐"
            except ValueError as impute_error:
                supported=False;reason=str(impute_error)
        out.append({**record(s),"counts":{role:sum(w['split']==role for w in snapshot['windows']) for role in ['train','cal','test']},"feature_count":len(snapshot['features']),"missing_cells":missing,"supported":supported,"reason":reason})
    return out
