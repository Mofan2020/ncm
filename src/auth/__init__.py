"""Authentication package."""

from src.auth.login import LoginManager, LoginMethod, LoginStatus, UserInfo, get_login_manager

__all__ = [
    "LoginManager",
    "LoginMethod",
    "LoginStatus",
    "UserInfo",
    "get_login_manager",
]
