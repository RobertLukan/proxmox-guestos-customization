from flask import render_template, request, Response, redirect, url_for, json, jsonify, flash
from app import app, db, celery, login_manager
from app.proxmox import get_template_vms, get_network_bridges, clone_vm_task, power_on_vm_task, wait_for_guest_agent_and_ip_task, reconfigure_vm_network_task, get_manageable_vms, get_vm_current_ip, prepare_reconfigure_task, get_vm_details, select_winrm_ip
from app.celery_app import sysprep_workflow_task, sysprep_existing_vm_task
from app.models import Task, User
from flask_login import login_user, logout_user, login_required, current_user
import uuid
import logging


def sanitized_domain_profiles():
    """Domain profiles safe to send to the browser (credentials stripped)."""
    return {
        name: {k: v for k, v in details.items() if k not in ('domain_password', 'domain_username')}
        for name, details in app.config.get('DOMAIN_PROFILES', {}).items()
    }


def _as_request_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 't', 'yes', 'on')


def _json_field_error(message, **field_errors):
    """Additive 400 JSON: keep ``error`` string and optional ``errors`` map."""
    payload = {'error': message}
    if field_errors:
        payload['errors'] = field_errors
    return jsonify(payload), 400


def apply_domain_profile_network(data):
    """Apply DNS / VLAN from the selected domain profile (independent of join).

    Selecting a profile is a network shortcut: it fills ``dns_servers`` and
    ``vlan`` when those fields were left blank. Credentials are never applied
    here — see ``resolve_domain_join_from_request``.

    Returns ``(True, None)`` on success, or ``(False, (response, status))`` when
    a non-empty profile name does not match a configured profile.
    """
    profile_name = (data.get('domain_profile') or '').strip()
    if not profile_name:
        return True, None
    profile = app.config.get('DOMAIN_PROFILES', {}).get(profile_name)
    if not profile:
        msg = f'Unknown domain profile: {profile_name!r}'
        return False, _json_field_error(msg, domain_profile=msg)
    if not (data.get('dns_servers') or '').strip() and profile.get('dns_servers'):
        data['dns_servers'] = profile.get('dns_servers')
    if data.get('vlan') in (None, '', 'None') and profile.get('vlan') is not None:
        data['vlan'] = profile.get('vlan')
    if not (data.get('domain_ou') or '').strip() and profile.get('domain_ou'):
        data['domain_ou'] = profile.get('domain_ou')
    return True, None


def resolve_domain_join_from_request(data):
    """Resolve domain-join fields on ``data`` server-side.

    Always applies network defaults from the selected profile (DNS/VLAN) first.
    When ``join_domain`` is set and ``use_domain_profile_credentials`` is true
    (the default), credentials and domain name are taken from ``DOMAIN_PROFILES``
    — never from the request body.

    Returns ``(True, None)`` on success, or ``(False, (response, status))`` when
    the named profile is missing.
    """
    ok, err = apply_domain_profile_network(data)
    if not ok:
        return False, err

    join_domain = _as_request_bool(data.get('join_domain'), False)
    data['join_domain'] = join_domain
    if not join_domain:
        data.pop('domain_password', None)
        return True, None

    use_profile = _as_request_bool(data.get('use_domain_profile_credentials'), True)
    data['use_domain_profile_credentials'] = use_profile
    if use_profile:
        profile_name = (data.get('domain_profile') or '').strip()
        profile = app.config.get('DOMAIN_PROFILES', {}).get(profile_name)
        if not profile:
            if not profile_name:
                msg = 'Select a domain profile when using profile credentials.'
            else:
                msg = f'Unknown domain profile: {profile_name!r}'
            return False, _json_field_error(msg, domain_profile=msg)
        data['domain_name'] = profile.get('domain_name')
        data['domain_username'] = profile.get('domain_username')
        data['domain_password'] = profile.get('domain_password')
    # else: domain_name / username / password come from the request body as typed
    return True, None


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        user = User.query.get(1) # Assuming user with id 1 exists
        if user and user.check_password(request.form['password']):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Invalid password')
    return render_template('login.html')

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        if not current_user.check_password(current_password):
            flash('Current password is incorrect.')
        elif len(new_password) < 8:
            flash('New password must be at least 8 characters long.')
        elif new_password != confirm_password:
            flash('New passwords do not match.')
        else:
            current_user.set_password(new_password)
            db.session.commit()
            flash('Password changed successfully.')
            return redirect(url_for('index'))
    return render_template('change_password.html')

@app.route('/reconfigure_network/<vmid>/<vm_uuid>')
@login_required
def reconfigure_network(vmid, vm_uuid):
    temp_ip_address = request.args.get('temp_ip_address')
    primary_mac_address = request.args.get('primary_mac_address') # New parameter

    current_ip_address = select_winrm_ip(vmid)

    # Never send secrets to the browser: strip domain credentials from the
    # profiles and do not pass WinRM credentials at all. The server resolves
    # the actual credentials in start_reconfigure_task.
    return render_template(
        'reconfigure_network.html',
        vmid=vmid,
        vm_uuid=vm_uuid,
        temp_ip_address=temp_ip_address,
        current_ip_address=current_ip_address,
        primary_mac_address=primary_mac_address,
        domain_profiles=sanitized_domain_profiles(),
    )

@app.route('/start_reconfigure_task', methods=['POST'], endpoint='start_reconfigure_task_endpoint')
@login_required
def start_reconfigure_task():
    data = request.json
    vmid = data.get('vmid')
    vm_uuid = data.get('vm_uuid')
    # temp_ip_address = data.get('temp_ip_address') # This will be determined dynamically
    primary_mac_address = data.get('primary_mac_address')
    new_ip_address = data.get('new_ip_address')
    netmask = data.get('netmask')
    gateway = data.get('gateway')
    dns_servers = data.get('dns_servers')
    remove_temp_interface = data.get('remove_temp_interface')
    vlan = data.get('vlan')

    # --- Resolve WinRM credentials server-side ---
    # By default use the predefined credentials from config; only fall back to
    # request-provided credentials when the operator explicitly opts out. Secrets
    # are never echoed back to (or trusted from) the browser by default.
    if data.get('use_predefined_winrm', True):
        winrm_username = app.config.get('WINRM_USERNAME')
        winrm_password = app.config.get('WINRM_PASSWORD')
    else:
        winrm_username = data.get('winrm_username')
        winrm_password = data.get('winrm_password')

    # --- Resolve domain-join parameters server-side ---
    ok, err = resolve_domain_join_from_request(data)
    if not ok:
        return err
    join_domain = data.get('join_domain', False)
    domain_name = data.get('domain_name')
    domain_username = data.get('domain_username')
    domain_password = data.get('domain_password')
    # Prefer DNS from the resolved data (may have been filled from the profile).
    if data.get('dns_servers'):
        dns_servers = data.get('dns_servers')

    # Determine the WinRM IP dynamically (first suitable address in WINRM_SUBNET).
    winrm_subnet_str = app.config.get('WINRM_SUBNET')
    selected_winrm_ip = select_winrm_ip(vmid)

    if not selected_winrm_ip:
        task_id = str(uuid.uuid4())
        task = Task(id=task_id, name='Reconfigure VM Network', description=f'Failed to find a suitable WinRM IP for VM {vmid}', vm_uuid=vm_uuid, status='FAILURE', message=f"Could not find a suitable WinRM IP address within the configured subnet ({winrm_subnet_str if winrm_subnet_str else 'any network'}).")
        db.session.add(task)
        db.session.commit()
        return jsonify({'task_id': task_id}), 500 # Return an error response

    temp_ip_address = selected_winrm_ip # Use the dynamically selected IP

    task_id = str(uuid.uuid4())
    task = Task(id=task_id, name='Reconfigure VM Network', description=f'Reconfiguring network for VM {vmid}', vm_uuid=vm_uuid)
    db.session.add(task)
    db.session.commit()

    reconfigure_vm_network_task.delay(
        task_id, vmid, vm_uuid, temp_ip_address, new_ip_address, netmask, gateway, 
        dns_servers, winrm_username, winrm_password, primary_mac_address, 
        remove_temp_interface, join_domain, domain_name, domain_username, domain_password,
        vlan
    )
    return jsonify({'task_id': task_id})

@app.route('/start_prepare_reconfigure_task', methods=['POST'])
@login_required
def start_prepare_reconfigure_task():
    data = request.json
    vmid = data.get('vmid')
    vm_uuid = data.get('vm_uuid')

    task_id = str(uuid.uuid4())
    task = Task(id=task_id, name='Prepare VM for Reconfiguration', description=f'Preparing VM {vmid} for network reconfiguration', vm_uuid=vm_uuid)
    db.session.add(task)
    db.session.commit()

    prepare_reconfigure_task.delay(task_id, vmid, vm_uuid)
    return jsonify({'task_id': task_id})

@app.route('/reconfigure_existing_vm')
@login_required
def reconfigure_existing_vm():
    vms = get_manageable_vms()
    return render_template('reconfigure_selection.html', vms=vms)

@app.route('/')
@login_required
def index():
    templates = get_template_vms()
    return render_template('index.html', templates=templates)

@app.route('/select', methods=['POST'])
@login_required
def select_template():
    template_vmid = request.form.get('template_vmid')
    return render_template('clone.html', template_vmid=template_vmid, domain_profiles=app.config['DOMAIN_PROFILES'])

@app.route('/start_clone_task', methods=['POST'], endpoint='start_clone_task_endpoint')
@login_required
def start_clone_task():
    try:
        data = request.json
        template_vmid = data.get('template_vmid')
        hostname = data.get('hostname')
        cores = int(data.get('cores'))
        ram = int(data.get('ram'))
        bridge = app.config['PRIMARY_BRIDGE']
        vlan = data.get('vlan')

        task_id = str(uuid.uuid4())
        vm_uuid = str(uuid.uuid4()) # Generate unique VM identifier
        task = Task(id=task_id, name='Clone VM', description=f'Cloning VM {hostname} from template {template_vmid}', vm_uuid=vm_uuid)
        
        try:
            db.session.add(task)
            db.session.commit()
        except Exception as e:
            logging.error(f"Error adding task to database: {e}")
            return jsonify({'error': 'Database error'}), 500

        try:
            clone_vm_task.delay(task_id, template_vmid, hostname, cores, ram, bridge, vlan)
        except Exception as e:
            logging.error(f"Error starting celery task: {e}")
            return jsonify({'error': 'Celery error'}), 500

        return jsonify({'task_id': task_id})
    except Exception as e:
        logging.error(f"Error in start_clone_task: {e}")
        return jsonify({'error': 'An unexpected error occurred.'}), 500

@app.route('/start_power_on_task', methods=['POST'])
@login_required
def start_power_on_task():
    data = request.json
    vmid = data.get('vmid')
    vm_uuid = data.get('vm_uuid') # Get vm_uuid from request

    task_id = str(uuid.uuid4())
    task = Task(id=task_id, name='Power On VM', description=f'Powering on VM {vmid}', vm_uuid=vm_uuid) # Store vm_uuid
    db.session.add(task)
    db.session.commit()

    power_on_vm_task.delay(task_id, vmid, vm_uuid) # Pass vm_uuid to task
    return jsonify({'task_id': task_id})

@app.route('/start_wait_for_ip_task', methods=['POST'])
@login_required
def start_wait_for_ip_task():
    data = request.json
    vmid = data.get('vmid')
    vm_uuid = data.get('vm_uuid') # Get vm_uuid from request

    task_id = str(uuid.uuid4())
    task = Task(id=task_id, name='Wait for IP', description=f'Waiting for IP for VM {vmid}', vm_uuid=vm_uuid) # Store vm_uuid
    db.session.add(task)
    db.session.commit()

    wait_for_guest_agent_and_ip_task.delay(task_id, vmid, vm_uuid) # Pass vm_uuid to task
    return jsonify({'task_id': task_id})

@app.route('/task_status/<task_id>')
@login_required
def task_status(task_id):
    task = Task.query.get(task_id)
    if task:
        return jsonify(task.to_dict())
    return jsonify({'error': 'Task not found'}), 404

@app.route('/workflow/<task_id>')
@login_required
def workflow(task_id):
    return render_template('workflow.html', task_id=task_id)

@app.route('/sysprep_form', methods=['POST'])
@login_required
def sysprep_form():
    template_vmid = request.form.get('template_vmid')
    bridges = get_network_bridges()
    return render_template(
        'sysprep_form.html',
        template_vmid=template_vmid,
        bridges=bridges,
        domain_profiles=sanitized_domain_profiles(),
    )

@app.route('/start_sysprep_workflow', methods=['POST'])
@login_required
def start_sysprep_workflow():
    data = request.json or {}
    ok, err = resolve_domain_join_from_request(data)
    if not ok:
        return err

    task_id = str(uuid.uuid4())
    vm_uuid = str(uuid.uuid4())
    hostname = data.get('hostname')

    task = Task(id=task_id, name='Sysprep Workflow', description=f'Starting Sysprep workflow for {hostname}', vm_uuid=vm_uuid)
    db.session.add(task)
    db.session.commit()

    sysprep_workflow_task.delay(task_id, data)

    return jsonify({'task_id': task_id})

@app.route('/sysprep_existing_vm_form/<vmid>')
@login_required
def sysprep_existing_vm_form(vmid):
    vm_details = get_vm_details(vmid)
    if not vm_details:
        return "VM not found", 404
    return render_template(
        'sysprep_existing_vm.html',
        vm=vm_details,
        domain_profiles=sanitized_domain_profiles(),
    )

@app.route('/start_sysprep_existing_vm_task', methods=['POST'])
@login_required
def start_sysprep_existing_vm_task():
    data = request.json or {}
    ok, err = resolve_domain_join_from_request(data)
    if not ok:
        return err

    vmid = data.get('vmid')
    hostname = data.get('hostname')
    task_id = str(uuid.uuid4())

    task = Task(id=task_id, name='Sysprep Existing VM', description=f'Starting Sysprep for VM {vmid} ({hostname})')
    db.session.add(task)
    db.session.commit()

    sysprep_existing_vm_task.delay(task_id, data)

    return jsonify({'task_id': task_id})
