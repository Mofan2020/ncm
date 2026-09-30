"""Authentication module for NetEase Cloud Music."""
from .login import LoginManager, LoginMethod, LoginStatus, get_login_manager

__all__ = ['LoginManager', 'LoginMethod', 'LoginStatus', 'get_login_manager']