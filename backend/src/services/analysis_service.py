import numpy as np
from ..constants.missing import DEFAULT_MISSING_POLICY, MissingPolicy, resolve_missing_policy
from .robust_algorithm import fit_train, score_with_fit, training_scale, finite_matrix
from .conformal_algorithm import upper_tail_p
from ..utils.fingerprint import fingerprint
VERSION='train-mcd-split-conformal-v2'
SPLITS=('train','cal','test')
ABS_LIMIT=1e9


def _number(value,label):
    """Return a measured float, or None for a missing observation. Invalid values always fail."""
    if value is None:return None
    if isinstance(value,(bool,np.bool_)) or isinstance(value,(complex,np.complexfloating)):raise ValueError(label+"不接受布尔或复数")
    if not isinstance(value,(int,float,np.integer,np.floating)):raise ValueError(label+"须为有限数值或留空")
    number=float(value)
    if not np.isfinite(number) or abs(number)>ABS_LIMIT:raise ValueError(label+"须为有限数值且绝对值不超过1e9")
    return number


def _feature_codes(snapshot):
    features=sorted(snapshot['features'],key=lambda f:f['position']);codes=[f['code'] for f in features]
    if len(features)!=4 or len(set(codes))!=4 or len({f['position'] for f in features})!=4:raise ValueError("须固定4个预定义且不重复的特征")
    return features,codes


def _read_rows(snapshot,codes):
    rows=sorted(snapshot['windows'],key=lambda w:w['code'])
    if not 25<=len(rows)<=2000 or len({w['code'] for w in rows})!=len(rows):raise ValueError("窗口数25–2000且编号不得重复")
    if len({w['id'] for w in rows})!=len(rows):raise ValueError("窗口身份重复，拆分不可交叠")
    normalized=[]
    for w in rows:
        if w['split'] not in SPLITS:raise ValueError("窗口角色仅train/cal/test")
        if set(w['values'])-set(codes):raise ValueError("每窗口不得包含预定义四量之外的值")
        values={c:_number(w['values'].get(c),f"窗口{w['code']} / {c}") for c in codes}
        normalized.append({**{k:v for k,v in w.items() if k!='values'},'values':values})
    return normalized


def missing_cells(snapshot):
    """Count absent/null measured cells without rejecting the study (used for the archive overview)."""
    try:features,codes=_feature_codes(snapshot)
    except ValueError:return 0
    return sum(1 for w in snapshot['windows'] for c in codes if w.get('values',{}).get(c) is None)


def prepare_snapshot(snapshot,missing_policy=DEFAULT_MISSING_POLICY):
    policy=resolve_missing_policy(missing_policy)
    features,codes=_feature_codes(snapshot);rows=_read_rows(snapshot,codes)
    groups={s:[] for s in SPLITS}
    for w in rows:groups[w['split']].append(w)
    if len(groups['train'])<20 or len(groups['cal'])<4 or len(groups['test'])<1:raise ValueError("严格拆分要求train≥20/cal≥4/test≥1")

    observed={s:{c:[] for c in codes} for s in SPLITS};per_split_missing={s:{c:0 for c in codes} for s in SPLITS};imputed_windows=[]
    for s in SPLITS:
        for w in groups[s]:
            flags=[]
            for c in codes:
                v=w['values'][c]
                if v is None:per_split_missing[s][c]+=1;flags.append(c)
                else:observed[s][c].append(v)
            if flags:imputed_windows.append({"code":w['code'],"split":s,"imputed":flags})
    if policy==MissingPolicy.REJECT.value:
        for s in SPLITS:
            for c in codes:
                if per_split_missing[s][c]:
                    w=next(w for w in groups[s] if w['values'][c] is None)
                    raise ValueError(f"严格缺测策略拒绝：{s} 窗口 {w['code']} 的 {c} 缺测；可改用训练中位数补齐")
        medians={c:None for c in codes}
    else:
        for c in codes:
            if not observed['train'][c]:raise ValueError(f"训练量 {c} 全缺：补值依据只取训练已测值，无法建立训练中位数")
        medians={c:float(np.median(observed['train'][c])) for c in codes}

    matrices={}
    for s in SPLITS:
        matrices[s]=finite_matrix([[w['values'][c] if w['values'][c] is not None else medians[c] for c in codes] for w in groups[s]],s)
    training_scale(matrices['train'])

    train_observed_counts={c:len(observed['train'][c]) for c in codes}
    applied=any(per_split_missing[s][c] for s in SPLITS for c in codes)
    imputation={"policy":policy,"method":"train_median" if policy==MissingPolicy.IMPUTE_TRAIN_MEDIAN.value else "reject","applied":applied,"feature_medians":medians,"train_observed_counts":train_observed_counts,"per_split_missing":per_split_missing,"counts":{s:sum(per_split_missing[s].values()) for s in SPLITS},"windows":imputed_windows,"source":"补值只取训练已测值的逐列中位数；不读取校准或测试值，训练某量全缺则拒绝。缺测位置按特征记为空值。"}
    return {"features":features,"codes":codes,"rows":rows,"groups":groups,"matrices":matrices,"imputation":imputation,"missing_policy":policy}


def analyze_deviation(snapshot,alpha=.05,seed=2026,missing_policy=DEFAULT_MISSING_POLICY):
    if type(alpha) not in (int,float) or not np.isfinite(alpha) or not .01<=alpha<=.2:raise ValueError("预设alpha须在.01–.2之间")
    prepared=prepare_snapshot(snapshot,missing_policy)
    features,rows,codes,groups,X,imputation=prepared['features'],prepared['rows'],prepared['codes'],prepared['groups'],prepared['matrices'],prepared['imputation']
    policy=prepared['missing_policy']
    imputed_by_code={w['code']:w['imputed'] for w in imputation['windows']}
    fit=fit_train(X['train'],seed);cal=score_with_fit(X['cal'],fit);test=score_with_fit(X['test'],fit);tail=upper_tail_p(cal,test)
    fit['raw_support_codes']=[w['code'] for w,m in zip(groups['train'],fit['raw_support_mask']) if m];fit['support_codes']=[w['code'] for w,m in zip(groups['train'],fit['support_mask']) if m]
    def used(split,index,feature_index):return float(X[split][index,feature_index])
    scores=[{"window":w,"used_values":{c:used('test',i,j) for j,c in enumerate(codes)},"imputed":imputed_by_code.get(w['code'],[]),"d2":float(d),"p":float(p),"cal_ge_count":int(k),"selected":bool(p<=alpha)} for i,(w,d,p,k) in enumerate(zip(groups['test'],test,tail['p'],tail['cal_ge_count']))]
    calibration=[{"window":w,"used_values":{c:used('cal',i,j) for j,c in enumerate(codes)},"imputed":imputed_by_code.get(w['code'],[]),"d2":float(d)} for i,(w,d) in enumerate(zip(groups['cal'],cal))]
    ecdf_values,ecdf_counts=np.unique(cal,return_counts=True);ecdf_y=np.cumsum(ecdf_counts)/len(cal)
    canonical={"study":snapshot['study'],"features":features,"windows":rows}
    train_used=[{"code":w['code'],"values":{c:used('train',i,j) for j,c in enumerate(codes)},"imputed":imputed_by_code.get(w['code'],[])} for i,w in enumerate(groups['train'])]
    fit_hash=fingerprint({"version":VERSION,"features":features,"train":train_used,"missing_policy":policy,"seed":seed,"support_fraction":.8})
    return {"algorithm_version":VERSION,"study":snapshot['study'],"features":features,"counts":{s:len(g) for s,g in groups.items()},"fit":fit,"fit_hash":fit_hash,"calibration":calibration,"test":scores,"calibration_ecdf":[{"d2":float(d),"cdf":float(y)} for d,y in zip(ecdf_values,ecdf_y)],"alpha":alpha,"seed":seed,"selected_count":sum(x['selected'] for x in scores),"resolution":tail['resolution'],"tail":tail['tail'],"missing_policy":policy,"imputation":imputation,"input_snapshot":canonical,"input_hash":fingerprint({"version":VERSION,"snapshot":canonical,"alpha":alpha,"seed":seed,"support_fraction":.8,"missing_policy":policy}),"assumptions":"MCD适用于单峰、近对称/椭圆主群，不适合多峰。定标与MCD只用train；固定训练且cal/test独立可交换时，上尾split-conformal提供边际误报控制，不是条件保证、FDR或危险诊断。","protocol":"四特征与alpha在运行前冻结；不以test选择特征、拟合、阈值或测试分位。筛选仅为人工独立补测线索。缺测处理方式同样在创建时冻结：补值只用训练已测值的逐列中位数，校准/测试缺测不改变拟合。"}
