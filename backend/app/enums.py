import enum


class ConnectorType(str, enum.Enum):
    CCS = "CCS"
    CHADEMO = "CHAdeMO"
    GBT = "GBT"


class MaintenanceKind(str, enum.Enum):
    DERATE = "DERATE"          # 降额：枪口可用但功率下调
    OUTAGE = "OUTAGE"          # 停用：窗口内完全不可用


class ReservationMode(str, enum.Enum):
    CONNECTOR = "CONNECTOR"    # 固定枪口：确认即锁定具体枪口
    POOL = "POOL"              # 资源池：只承诺名额，到站再分配枪口


class ReservationStatus(str, enum.Enum):
    CONFIRMED = "CONFIRMED"                    # 已确认，持有容量
    ARRIVED = "ARRIVED"                        # 已到站（池模式此时分配枪口）
    CHARGING = "CHARGING"                      # 充电中，预约管理不可直接释放
    COMPLETED = "COMPLETED"                    # 已完成
    CANCELLED = "CANCELLED"                    # 已取消，释放容量
    NO_SHOW = "NO_SHOW"                        # 迟到失约，释放容量
    NEEDS_RESELECTION = "NEEDS_RESELECTION"    # 维护影响：需改选
    MANUAL_CONTACT = "MANUAL_CONTACT"          # 维护影响：需人工联系


# 仍持有服务容量、会在容量计算中占位的状态
HOLDING_STATUSES = (
    ReservationStatus.CONFIRMED,
    ReservationStatus.ARRIVED,
    ReservationStatus.CHARGING,
)


class ServiceActionKind(str, enum.Enum):
    ALTERNATIVE_CONNECTOR = "ALTERNATIVE_CONNECTOR"  # 客服指定替代枪口
    FEE_ADJUSTMENT = "FEE_ADJUSTMENT"                # 费用调整
    NOTE = "NOTE"                                    # 人工联系备注


class ImpactDecision(str, enum.Enum):
    UNCHANGED = "UNCHANGED"                      # 仍能履约，订单不动
    NEEDS_RESELECTION = "NEEDS_RESELECTION"      # 进入改选
    MANUAL_CONTACT = "MANUAL_CONTACT"            # 已到站，人工联系
    PROTECTED_CHARGING = "PROTECTED_CHARGING"    # 充电中，受保护不可释放


# ---------- 候补 ----------

class WaitlistStatus(str, enum.Enum):
    QUEUED = "QUEUED"                    # 排队中，等待空档邀请
    INVITED = "INVITED"                  # 已收到一次性邀请，等待在截止前响应
    CONFIRMED = "CONFIRMED"              # 接受邀请并已生成确认预约（终态）
    DECLINED = "DECLINED"                # 司机拒绝邀请，回到队尾继续候补
    EXPIRED = "EXPIRED"                  # 邀请超时未响应，回到队尾继续候补
    CANCELLED = "CANCELLED"              # 司机撤销候补（终态）
    CLOSED = "CLOSED"                    # 超过到站期限仍未成交，系统关闭（终态）


# 仍处于候补队列、可继续接收邀请的状态
WAITLIST_OPEN_STATUSES = (WaitlistStatus.QUEUED, WaitlistStatus.DECLINED, WaitlistStatus.EXPIRED)
# 候补生命周期终态
WAITLIST_TERMINAL_STATUSES = (
    WaitlistStatus.CONFIRMED,
    WaitlistStatus.CANCELLED,
    WaitlistStatus.CLOSED,
)


class InviteStatus(str, enum.Enum):
    PENDING = "PENDING"        # 待响应（在截止时刻之前）
    ACCEPTED = "ACCEPTED"      # 已接受并确认（一次性，全局唯一一笔）
    DECLINED = "DECLINED"      # 司机明确拒绝
    EXPIRED = "EXPIRED"        # 超过截止时刻未响应
    SUPERSEDED = "SUPERSEDED"  # 人工保留/撤销等运营动作使其失效


class MatchPolicy(str, enum.Enum):
    # 严格按加入时间（同站点同策略域内 created_at, id 升序），公平易解释
    FIFO = "FIFO"
    # 兼容优先：先安排只有少数枪口能满足的“严格”请求，再按排队时间处理同等候选
    COMPATIBILITY = "COMPATIBILITY"
