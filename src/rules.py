from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
EQUIPMENT_ROLES=set(['applicant', 'inspector'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
# 续期申请状态机：待批 -> 已批 / 退回复核；退回 -> 待批（重新提交）
RENEWAL_STATES=['pending', 'approved', 'returned']
RENEWAL_TRANSITIONS={'pending': ['approved', 'returned'], 'returned': ['pending'], 'approved': []}
RENEWAL_ROLES={'approved': ['compliance_manager'], 'pending': ['applicant'], 'returned': []}
# 快照/修订来源与重算原因
SNAPSHOT_REASONS=['initial', 'renewal_submitted', 'renewal_resubmitted', 'equipment_changed', 'record_changed', 'record_closed']
SNAPSHOT_SOURCES=['system', 'backfill', 'renewal', 'equipment', 'record']
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
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def can_transition_renewal(current,target): return target in RENEWAL_TRANSITIONS.get(current,[])
def validate_renewal_transition(current,target):
    if current not in RENEWAL_STATES or target not in RENEWAL_STATES: raise ValidationError("未知续期状态")
    if not can_transition_renewal(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def role_for_renewal_transition(target): return set(RENEWAL_ROLES.get(target,[]))
def is_compliant(quantity,threshold,open_records):
    """排放未超限且无未关闭事项视为合规。"""
    emission_ok = threshold <= 0 or quantity < threshold
    return bool(emission_ok) and int(open_records) == 0
def renewal_blockers(quantity,threshold,open_records):
    """待批申请退回复核的原因列表。"""
    blockers=[]
    if threshold > 0 and quantity >= threshold: blockers.append("排放量超过许可限值")
    if int(open_records) > 0: blockers.append("仍有未关闭事项")
    return blockers
