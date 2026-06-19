PINGS_PANEL_IMAGE = (
    "https://cdn.discordapp.com/attachments/1517430214584700950/"
    "1517431114346528808/actividades-gremiales.png?ex=6a36415a&is=6a34efda&"
    "hm=812245ba88e45a021b9dc03f2788eae834ccb3913fcc6fc8d9afc4c379121c43"
)

BANK_PANEL_IMAGE = (
    "https://cdn.discordapp.com/attachments/1517430214584700950/"
    "1517430793780072479/G3N-BANCO.png?ex=6a36410d&is=6a34ef8d&"
    "hm=1f42537fe22b204b8112412b16c3ca5d1bccb54cbd6533358a3adbc1f56891c3"
)

ADMIN_PANEL_IMAGE = (
    "https://cdn.discordapp.com/attachments/1517430214584700950/"
    "1517431097011736596/panel_administrativo.png?ex=6a364156&is=6a34efd6&"
    "hm=1543c9ab081d7167558c55eb5c78d16b815fae66b9ae93bfa10c8006ac51dfae"
)

CALLERS_WELCOME_IMAGE = (
    "https://cdn.discordapp.com/attachments/1496878843644870716/"
    "1517005391458996295/callers.jpg?ex=6a34b4dd&is=6a33635d&"
    "hm=81fb1fe926eefbc1453299848aba21dda9730f12c13171536154bbf2dc755568"
)

ACTIVITY_DRAFT = "Borrador"
ACTIVITY_OPEN = "Abierta"
ACTIVITY_NOTICE = "En aviso"
ACTIVITY_IN_PROGRESS = "En curso"
ACTIVITY_CANCELLED = "Cancelada"
ACTIVITY_FINISHED = "Finalizada"
ACTIVITY_PAYOUT_CREATED = "Reparto generado"

ACTIVITY_STATUSES = {
    ACTIVITY_DRAFT,
    ACTIVITY_OPEN,
    ACTIVITY_NOTICE,
    ACTIVITY_IN_PROGRESS,
    ACTIVITY_CANCELLED,
    ACTIVITY_FINISHED,
    ACTIVITY_PAYOUT_CREATED,
}

ATTENDANCE_CONFIRMED = "Confirmado"
ATTENDANCE_ABSENT = "Ausente"
ATTENDANCE_JUSTIFIED = "Justificado"
ATTENDANCE_PENDING = "Pendiente"

FINE_PENDING = "Pendiente"
FINE_PAID = "Pagada"
FINE_CANCELLED = "Cancelada"
FINE_SEIZED = "Decomisada"

WITHDRAWAL_PENDING = "Pendiente"
WITHDRAWAL_APPROVED = "Aprobada"
WITHDRAWAL_REJECTED = "Rechazada"
WITHDRAWAL_LIQUIDATED = "Liquidada"
WITHDRAWAL_PARTIAL = "Liquidada parcialmente"
WITHDRAWAL_CANCELLED = "Cancelada"

PAYOUT_PENDING = "Pendiente"
PAYOUT_APPROVED = "Aprobado"
PAYOUT_REJECTED = "Rechazado"
PAYOUT_CORRECTION = "Correccion solicitada"
PAYOUT_DEPOSITED = "Depositos realizados"
PAYOUT_CANCELLED = "Cancelado"

DEFAULT_MEMBER_ROLE_NAME = "MIEMBRO G3NESYS"
DEFAULT_GUEST_ROLE_NAME = "INVITADO"

DEFAULT_SETTINGS = {
    "channel_pings_id": "",
    "channel_admin_id": "",
    "channel_cobros_id": "",
    "channel_multas_id": "",
    "channel_historial_id": "",
    "channel_repartos_id": "",
    "channel_notify_splits_id": "",
    "channel_notify_withdrawals_id": "",
    "channel_notify_registration_id": "",
    "channel_notify_activities_id": "",
    "channel_notify_fines_id": "",
    "channel_notify_general_admin_id": "",
    "admin_role_ids": "",
    "fine_moderator_role_ids": "",
    "payout_approver_role_ids": "",
    "withdrawal_liquidator_role_ids": "",
    "member_role_name": DEFAULT_MEMBER_ROLE_NAME,
    "guest_role_name": DEFAULT_GUEST_ROLE_NAME,
    "guild_percentage_default": "10",
    "market_rate_default": "0",
    "absence_fine_amount": "0",
    "absence_fine_enabled": "1",
    "minimum_withdrawal": "0",
    "currency_name": "plata",
    "notice_minutes": "15",
    "attendance_check_minutes": "10",
    "voice_minimum_percent": "50",
    "caller_percentage_default": "0",
    "consecutive_absence_limit": "3",
    "total_absence_limit": "10",
    "pending_fine_penalty_limit": "3",
    "transfer_fee_percent": "3",
    "require_voice_for_attendance": "0",
}
