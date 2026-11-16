"""Settings → Tracking accounts: Simkl sign-in and Floppy/Simkl connection tests."""
from flask import Blueprint, jsonify

from .models import admin_required

tracker_account_bp = Blueprint('tracker_accounts', __name__)


@tracker_account_bp.route('/simkl/auth/start', methods=['POST'])
@admin_required
def simkl_auth_start():
    from content_checkers.simkl import start_device_auth
    return jsonify(start_device_auth())


@tracker_account_bp.route('/simkl/auth/poll', methods=['POST'])
@admin_required
def simkl_auth_poll():
    from content_checkers.simkl import poll_device_auth
    return jsonify(poll_device_auth())


@tracker_account_bp.route('/simkl/auth/status', methods=['GET'])
@admin_required
def simkl_auth_status():
    from content_checkers.simkl import auth_status
    return jsonify(auth_status())


@tracker_account_bp.route('/simkl/auth/signout', methods=['POST'])
@admin_required
def simkl_auth_signout():
    from content_checkers.simkl import sign_out
    sign_out()
    return jsonify({'success': True})


@tracker_account_bp.route('/floppy/test', methods=['POST'])
@admin_required
def floppy_test():
    from content_checkers.floppy import test_connection
    ok, message = test_connection()
    return jsonify({'success': ok, 'message': message})
