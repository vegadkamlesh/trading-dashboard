"""Account identity and selection. Data access never depends on this context."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Literal

from fastapi import HTTPException

AccountId = Literal["master", "child"]
ACCOUNT_IDS: tuple[AccountId, ...] = ("master", "child")
_current: ContextVar[AccountId] = ContextVar("trading_account", default="master")
_selected: tuple[AccountId, ...] = ("master",)


def current_account() -> AccountId:
    return _current.get()


@contextmanager
def account_scope(account: AccountId) -> Iterator[None]:
    token = _current.set(account)
    try:
        yield
    finally:
        _current.reset(token)


def account_credentials(account: AccountId) -> tuple[str, str]:
    from .config import get_settings
    s = get_settings()
    if account == "child":
        return s.dhan_child_client_id, s.dhan_child_access_token
    return s.dhan_client_id, s.dhan_master_trading_token or s.dhan_access_token


def account_name(account: AccountId) -> str:
    return "Master Dhan" if account == "master" else "Child Dhan"


def selected_accounts() -> tuple[AccountId, ...]:
    return _selected


def select_accounts(accounts: list[AccountId]) -> None:
    global _selected
    if len(accounts) != len(set(accounts)) or any(a not in ACCOUNT_IDS for a in accounts):
        raise HTTPException(400, "Choose unique Master/Child accounts")
    for account in accounts:
        if not all(account_credentials(account)):
            raise HTTPException(409, f"{account_name(account)} trading credentials are not configured")
    _selected = tuple(a for a in ACCOUNT_IDS if a in accounts)
