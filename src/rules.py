from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction','submitted'], 'correction': ['approved','submitted']}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); EQUIPMENT_ROLES=set(['applicant']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
# 许可快照：提交时冻结排放量与治理设备版本，之后任何重算都产生新版本
PENDING_STATES=('submitted','inspection','correction')
SNAP_INITIAL='initial'; SNAP_SUBMIT='submit'; SNAP_RECALC='recalculate'; SNAP_REVISION='revision'
SNAP_SOURCES=(SNAP_INITIAL,SNAP_SUBMIT,SNAP_RECALC,SNAP_REVISION)
EFFECT_INITIAL='initial'; EFFECT_FROZEN='frozen'; EFFECT_REFRESHED='refreshed'; EFFECT_RETURNED='returned'; EFFECT_REVISION='revision'
TRIGGER_BACKFILL='backfill'; TRIGGER_SUBMIT='submit'; TRIGGER_INSPECTION='inspection_record'; TRIGGER_EQUIPMENT='equipment_change'; TRIGGER_RECORD_CLOSE='record_close'
TRIGGER_LABELS={TRIGGER_BACKFILL:'历史数据补齐初始版本',TRIGGER_SUBMIT:'续期申请提交冻结',TRIGGER_INSPECTION:'检查记录变更',TRIGGER_EQUIPMENT:'治理设备变更',TRIGGER_RECORD_CLOSE:'整改事项关闭'}
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records,exceedance=False):
    blockers=[]
    if target in TERMINAL_STATES:
        if open_records>0: blockers.append("仍有未关闭事项")
        if exceedance: blockers.append("存在超标检查记录")
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def build_reasons(*,equipment_version,frozen_equipment_version,open_records,exceedance,
                  max_measured,frozen_threshold,trigger,changed=False):
    """重算原因：同时用于列表展示与审计留痕。"""
    reasons=[]
    if equipment_version!=frozen_equipment_version:
        reasons.append(f"治理设备版本由v{frozen_equipment_version}变更为v{equipment_version}")
    if open_records>0:
        reasons.append(f"存在{open_records}条未关闭检查事项")
    if exceedance:
        if max_measured is not None:
            reasons.append(f"实测排放量{max_measured:g}超过许可限值{frozen_threshold:g}")
        else:
            reasons.append("检查发现排放超标")
    if not reasons:
        label=TRIGGER_LABELS.get(trigger,trigger)
        reasons.append(f"{label}触发重算，排放量、设备与事项指标正常" if changed else f"{label}，指标正常")
    return reasons
def is_blocking(open_records,exceedance,equipment_drift=False):
    return open_records>0 or exceedance
def recompute_effect(status,open_records,exceedance,equipment_drift):
    """根据申请所处阶段给出重算结论：已批→修订版，待批且超标/有未关闭事项→退回复核。"""
    blocking=is_blocking(open_records,exceedance,equipment_drift)
    if status=='approved':
        return EFFECT_REVISION if (blocking or equipment_drift) else EFFECT_REFRESHED
    if status in PENDING_STATES:
        return EFFECT_RETURNED if blocking else EFFECT_REFRESHED
    return None
def effect_source(effect):
    return SNAP_REVISION if effect==EFFECT_REVISION else SNAP_RECALC
