"""Тесты worker._user_error_message — дружелюбные тексты ошибок."""
from worker import _user_error_message

# Реальный ответ apinet, который пугал юзера сырым JSON (2026-06-14):
_QUOTA_403 = (
    "Error code: 403 - {'error': {'message': '预扣费额度失败, 用户剩余额度: "
    "＄0.037350, 需要预扣费额度: ＄0.037826', 'type': 'new_api_error', "
    "'param': '', 'code': 'insufficient_user_quota'}}"
)
_BALANCE_402 = "Error code: 402 - Insufficient USD balance"

_BILLING_HINT = "На балансе apinet закончились средства"


def test_quota_403_insufficient_user_quota_is_friendly():
    """403 insufficient_user_quota → подсказка пополнить, а не сырой JSON."""
    msg = _user_error_message(_QUOTA_403)
    assert _BILLING_HINT in msg
    assert "403" not in msg and "insufficient_user_quota" not in msg


def test_quota_403_matched_by_chinese_phrase():
    """Даже без машинного кода ловим по китайской фразе «предоплатное списание»."""
    assert _BILLING_HINT in _user_error_message("预扣费额度失败")


def test_balance_402_still_friendly():
    """Старый кейс 402 Insufficient USD balance не сломан."""
    assert _BILLING_HINT in _user_error_message(_BALANCE_402)


def test_generic_error_passes_through():
    """Прочие ошибки показываем как есть (с префиксом)."""
    msg = _user_error_message("boom: connection reset")
    assert msg == "Не получилось обработать: boom: connection reset"
    assert _BILLING_HINT not in msg
