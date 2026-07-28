/**
 * Shared helpers for the Sysprep wizard form.
 * Payload field names must stay aligned with start_* routes.
 */
(function (global) {
  'use strict';

  function showFormAlert(el, message, fieldErrors) {
    if (!el) return;
    el.className = 'alert alert-danger';
    var parts = [];
    if (message) parts.push(message);
    if (fieldErrors && typeof fieldErrors === 'object') {
      Object.keys(fieldErrors).forEach(function (k) {
        parts.push(k + ': ' + fieldErrors[k]);
      });
    }
    el.textContent = parts.join(' ') || 'Request failed.';
  }

  function clearFormAlert(el) {
    if (!el) return;
    el.textContent = '';
    el.className = 'alert alert-danger';
  }

  function applyDomainProfile(selectEl, profiles, dnsEl, vlanEl) {
    var name = selectEl && selectEl.value;
    if (name && profiles && profiles[name]) {
      var p = profiles[name];
      if (dnsEl && p.dns_servers !== undefined) dnsEl.value = p.dns_servers || '';
      if (vlanEl && p.vlan !== undefined && p.vlan !== null && p.vlan !== '') {
        vlanEl.value = p.vlan;
      }
    } else {
      if (dnsEl) dnsEl.value = '';
      if (vlanEl) vlanEl.value = '';
    }
  }

  function setSectionVisible(el, visible) {
    if (!el) return;
    el.style.display = visible ? '' : 'none';
    el.hidden = !visible;
  }

  function setDataRequired(els, on) {
    (els || []).forEach(function (el) {
      if (!el) return;
      if (on) el.setAttribute('data-required', 'true');
      else {
        el.removeAttribute('data-required');
        if (window.GuestOSValidate) GuestOSValidate.clearValidation(el);
      }
    });
  }

  function wireNetworkMode(form) {
    var mode = form.querySelector('#network_mode');
    var staticFields = form.querySelector('#static_fields');
    var staticInputs = form.querySelectorAll('#ip_address, #netmask_cidr, #gateway, #new_ip_address, #netmask');
    function apply() {
      var isDhcp = mode && mode.value === 'dhcp';
      setSectionVisible(staticFields, !isDhcp);
      setDataRequired(Array.prototype.slice.call(staticInputs), !isDhcp);
    }
    if (mode) mode.addEventListener('change', apply);
    apply();
    return apply;
  }

  function wireDomainJoin(form) {
    var joinCb = form.querySelector('#join_domain_checkbox');
    var domainFields = form.querySelector('#domain_fields');
    var useProfileCb = form.querySelector('#use_domain_profile_credentials');
    var credFields = form.querySelector('#domain_credentials_fields');
    var profileSelect = form.querySelector('#domain_profile');
    var manualCreds = form.querySelectorAll('#domain_name, #domain_username, #domain_password');

    function applyCreds() {
      var joining = joinCb && joinCb.checked;
      var useProfile = !useProfileCb || useProfileCb.checked;
      setSectionVisible(credFields, joining && !useProfile);
      setDataRequired(Array.prototype.slice.call(manualCreds), joining && !useProfile);
      if (profileSelect) {
        if (joining && useProfile) profileSelect.setAttribute('data-required', 'true');
        else profileSelect.removeAttribute('data-required');
      }
    }

    function applyJoin() {
      var joining = joinCb && joinCb.checked;
      setSectionVisible(domainFields, joining);
      applyCreds();
    }

    if (joinCb) joinCb.addEventListener('change', applyJoin);
    if (useProfileCb) useProfileCb.addEventListener('change', applyCreds);
    applyJoin();
    return { applyJoin: applyJoin, applyCreds: applyCreds };
  }

  function wireManageDisks(form) {
    var manageCb = form.querySelector('#manage_disks_checkbox');
    var diskFields = form.querySelector('#disk_fields');
    var pagefileCb = form.querySelector('#ensure_pagefile_disk');
    var dataCb = form.querySelector('#ensure_data_disk');
    var pagefileFields = form.querySelector('#pagefile_disk_fields');
    var dataFields = form.querySelector('#data_disk_fields');

    function apply() {
      var on = manageCb && manageCb.checked;
      setSectionVisible(diskFields, on);
      setSectionVisible(pagefileFields, on && (!pagefileCb || pagefileCb.checked));
      setSectionVisible(dataFields, on && (!dataCb || dataCb.checked));
    }
    if (manageCb) manageCb.addEventListener('change', apply);
    if (pagefileCb) pagefileCb.addEventListener('change', apply);
    if (dataCb) dataCb.addEventListener('change', apply);
    apply();
    return apply;
  }

  function collectSysprepPayload(form) {
    var data = Object.fromEntries(new FormData(form).entries());
    var joinDomain = !!(form.querySelector('#join_domain_checkbox') || {}).checked;
    var useProfileCreds = !!(form.querySelector('#use_domain_profile_credentials') || {}).checked;
    data.join_domain = joinDomain;
    data.use_domain_profile_credentials = useProfileCreds;
    if (!joinDomain || useProfileCreds) {
      data.domain_name = '';
      data.domain_username = '';
      data.domain_password = '';
    }

    var disksPanel = form.querySelector('#disks-wizard-panel');
    var disksDisabled = disksPanel && disksPanel.getAttribute('data-wizard-disabled') === 'true';
    var manageDisks = !disksDisabled && !!(form.querySelector('#manage_disks_checkbox') || {}).checked;
    data.manage_disks = manageDisks;
    delete data.os_grow_to_gb;
    delete data.pagefile_size_gb;
    delete data.pagefile_drive_letter;
    delete data.data_size_gb;
    delete data.data_drive_letter;
    if (manageDisks) {
      var disks = [{ role: 'os' }];
      var grow = (form.querySelector('#os_grow_to_gb') || {}).value;
      if (grow) disks[0].grow_to_gb = parseInt(grow, 10);
      if ((form.querySelector('#ensure_pagefile_disk') || {}).checked) {
        disks.push({
          role: 'pagefile',
          size_gb: parseInt((form.querySelector('#pagefile_size_gb') || {}).value || '16', 10),
          drive_letter: ((form.querySelector('#pagefile_drive_letter') || {}).value || 'P'),
          ensure_pagefile: true,
        });
      }
      if ((form.querySelector('#ensure_data_disk') || {}).checked) {
        disks.push({
          role: 'data',
          size_gb: parseInt((form.querySelector('#data_size_gb') || {}).value || '50', 10),
          drive_letter: ((form.querySelector('#data_drive_letter') || {}).value || 'D'),
          label: 'Data',
        });
      }
      data.disks = disks;
    } else {
      data.disks = [];
    }
    return data;
  }

  function isUsableHostIPv4(ip) {
    var parts = String(ip || '').trim().split('.');
    if (parts.length !== 4) return false;
    var octets = [];
    for (var i = 0; i < 4; i++) {
      if (!/^\d+$/.test(parts[i])) return false;
      var n = Number(parts[i]);
      if (n < 0 || n > 255) return false;
      octets.push(n);
    }
    if (octets[0] === 0) return false; // 0.0.0.0/8
    if (octets[0] === 127) return false; // loopback
    if (octets[0] === 169 && octets[1] === 254) return false; // link-local
    if (octets[0] >= 224) return false; // multicast / reserved
    return true;
  }

  function isValidBulkHostname(name) {
    var label = String(name || '').trim().split('.')[0];
    return /^[A-Za-z0-9-]{1,15}$/.test(label) && !/^-/.test(label) && !/-$/.test(label);
  }

  function parseBulkRows(text, mode) {
    var lines = (text || '').split(/\r?\n/);
    var rows = [];
    var isDhcp = mode === 'dhcp';
    var inferredPrefix = '';
    var seenHost = {};
    var seenIp = {};
    var errors = [];

    function parseIpWithOptionalPrefix(value) {
      var raw = (value || '').trim();
      if (!raw) return { ip: '', prefix: '' };
      var parts = raw.split('/');
      var ip = (parts[0] || '').trim();
      var prefix = (parts[1] || '').trim();
      return { ip: ip, prefix: prefix };
    }

    lines.forEach(function (line, idx) {
      var lineNo = idx + 1;
      var raw = (line || '').trim();
      if (!raw) return;
      var parts = raw.split(',').map(function (p) { return p.trim(); });
      var hostname = parts[0] || '';
      if (!hostname) {
        errors.push('Line ' + lineNo + ': hostname is required.');
        return;
      }
      if (!isValidBulkHostname(hostname)) {
        errors.push(
          'Line ' + lineNo + ': invalid hostname "' + hostname +
            '" (1–15 letters/digits/hyphen).'
        );
        return;
      }
      var hostKey = hostname.toLowerCase();
      if (seenHost[hostKey]) {
        errors.push(
          'Duplicate hostname "' + hostname + '" on lines ' +
            seenHost[hostKey] + ' and ' + lineNo + '.'
        );
        return;
      }
      seenHost[hostKey] = lineNo;

      var row = { hostname: hostname.split('.')[0] };
      if (isDhcp) {
        if (parts[1]) {
          var vlanDhcp = parseInt(parts[1], 10);
          if (!Number.isInteger(vlanDhcp) || vlanDhcp < 1 || vlanDhcp > 4094) {
            errors.push('Line ' + lineNo + ': VLAN must be 1–4094.');
            return;
          }
          row.vlan = String(vlanDhcp);
        }
      } else {
        if (!parts[1]) {
          errors.push('Line ' + lineNo + ': static mode requires ip/prefix.');
          return;
        }
        var parsed = parseIpWithOptionalPrefix(parts[1]);
        if (!parsed.ip || !isUsableHostIPv4(parsed.ip)) {
          errors.push(
            'Line ' + lineNo + ': invalid or unusable IP "' + (parsed.ip || parts[1]) +
              '" (no loopback/link-local/multicast).'
          );
          return;
        }
        if (seenIp[parsed.ip]) {
          errors.push(
            'Duplicate IP ' + parsed.ip + ' on lines ' +
              seenIp[parsed.ip] + ' and ' + lineNo + '.'
          );
          return;
        }
        seenIp[parsed.ip] = lineNo;
        row.ip_address = parsed.ip;
        if (!parsed.prefix) {
          errors.push('Line ' + lineNo + ': CIDR prefix required (e.g. ' + parsed.ip + '/24).');
          return;
        }
        if (!/^\d+$/.test(parsed.prefix)) {
          errors.push('Line ' + lineNo + ': CIDR prefix must look like /24.');
          return;
        }
        var prefixInt = parseInt(parsed.prefix, 10);
        if (prefixInt < 1 || prefixInt > 32) {
          errors.push('Line ' + lineNo + ': CIDR must be between /1 and /32.');
          return;
        }
        if (!inferredPrefix) inferredPrefix = String(prefixInt);
        if (inferredPrefix !== String(prefixInt)) {
          errors.push(
            'Line ' + lineNo + ': all rows must use the same CIDR prefix (/' +
              inferredPrefix + ').'
          );
          return;
        }
        if (parts[2]) {
          var vlanStatic = parseInt(parts[2], 10);
          if (!Number.isInteger(vlanStatic) || vlanStatic < 1 || vlanStatic > 4094) {
            errors.push('Line ' + lineNo + ': VLAN must be 1–4094.');
            return;
          }
          row.vlan = String(vlanStatic);
        }
      }
      rows.push(row);
    });

    if (errors.length) {
      var err = new Error(errors.slice(0, 5).join(' '));
      err.bulkErrors = errors;
      throw err;
    }
    return { rows: rows, prefix: inferredPrefix, errors: [] };
  }

  function validateBulkCsvText(text, mode) {
    try {
      return { ok: true, parsed: parseBulkRows(text, mode), errors: [] };
    } catch (e) {
      return {
        ok: false,
        parsed: null,
        errors: e.bulkErrors || [e.message || 'Invalid bulk CSV.'],
      };
    }
  }

  function estimateRequestedDiskGb(form) {
    var manage = !!(form.querySelector('#manage_disks_checkbox') || {}).checked;
    if (!manage) return 0;
    var total = 0;
    var grow = (form.querySelector('#os_grow_to_gb') || {}).value;
    if (grow) {
      var g = parseInt(grow, 10);
      if (!isNaN(g)) total += g;
    }
    if ((form.querySelector('#ensure_pagefile_disk') || {}).checked) {
      var pf = parseInt((form.querySelector('#pagefile_size_gb') || {}).value || '16', 10);
      if (!isNaN(pf)) total += pf;
    }
    if ((form.querySelector('#ensure_data_disk') || {}).checked) {
      var data = parseInt((form.querySelector('#data_size_gb') || {}).value || '50', 10);
      if (!isNaN(data)) total += data;
    }
    return total;
  }

  function estimateBulkDiskSumGb(form, itemCount) {
    var perVm = estimateRequestedDiskGb(form);
    var n = Math.max(0, parseInt(itemCount, 10) || 0);
    return { per_vm_gb: perVm, items: n, batch_sum_gb: perVm * n };
  }

  function collectBulkPayload(form) {
    var shared = collectSysprepPayload(form);
    var modeEl = form.querySelector('#bulk_network_input_mode');
    var mode = modeEl && modeEl.value ? modeEl.value : 'static';
    var rowsText = (form.querySelector('#bulk_rows') || {}).value || '';
    var parsed = parseBulkRows(rowsText, mode);
    var rows = parsed.rows;
    var bulkGateway = (form.querySelector('#bulk_gateway') || {}).value || '';
    var bulkDns = (form.querySelector('#bulk_dns_servers') || {}).value || '';
    // In bulk mode the per-row list owns hostnames/IPs.
    shared.hostname = '';
    shared.ip_address = '';
    shared.domain_profile = '';
    shared.vlan = '';
    shared.dns_servers = bulkDns.trim();
    if (mode === 'dhcp') {
      shared.network_mode = 'dhcp';
      shared.gateway = '';
      shared.netmask_cidr = '';
    } else {
      shared.network_mode = 'static';
      shared.gateway = bulkGateway.trim();
      shared.netmask_cidr = parsed.prefix || shared.netmask_cidr || '24';
      if (!shared.gateway) {
        throw new Error('Bulk static mode requires a default gateway.');
      }
      if (!isUsableHostIPv4(shared.gateway)) {
        throw new Error('Default gateway must be a usable IPv4 address.');
      }
      rows.forEach(function (r) {
        if (r.ip_address && r.ip_address === shared.gateway) {
          throw new Error(
            'Default gateway ' + shared.gateway +
              ' collides with hostname ' + r.hostname + ' IP.'
          );
        }
      });
    }
    return {
      shared: shared,
      items: rows,
    };
  }

  function fillReview(form, target) {
    if (!target) return;
    var htmlRows = [];
    function add(label, value) {
      htmlRows.push('<dt>' + label + '</dt><dd>' + (value || '—') + '</dd>');
    }
    var provisionMode = form.querySelector('#provision_mode');
    var isBulk = provisionMode && provisionMode.value === 'bulk';
    var hostname = form.querySelector('#hostname');
    if (!isBulk && hostname) add('Hostname', hostname.value);
    if (isBulk) {
      var bulkMode = (form.querySelector('#bulk_network_input_mode') || {}).value || 'static';
      var bulkCheck = validateBulkCsvText(
        (form.querySelector('#bulk_rows') || {}).value || '',
        bulkMode
      );
      if (!bulkCheck.ok) {
        add('CSV validation', bulkCheck.errors.slice(0, 3).join(' '));
      } else {
        var parsed = bulkCheck.parsed;
        var desktopRows = parsed.rows || [];
        add('Provision mode', 'Bulk');
        add('Desktop rows', String(desktopRows.length));
        add('Row network mode', bulkMode);
        if (bulkMode === 'static') {
          add('CIDR prefix', parsed.prefix || (form.querySelector('#netmask_cidr') || {}).value);
          add('Gateway', (form.querySelector('#bulk_gateway') || {}).value);
        }
        add('DNS', (form.querySelector('#bulk_dns_servers') || {}).value);
        var hostnames = desktopRows.map(function (r) { return r.hostname; }).filter(Boolean);
        if (hostnames.length) {
          add('Hostnames', hostnames.join(', '));
        }
        var diskEst = estimateBulkDiskSumGb(form, desktopRows.length);
        if (diskEst.per_vm_gb > 0) {
          add(
            'Requested disks (info)',
            diskEst.per_vm_gb + ' GB × ' + diskEst.items +
              ' = ' + diskEst.batch_sum_gb + ' GB (display only; not enforced)'
          );
        } else {
          add('Requested disks (info)', 'None (Configure disks off / thin clone)');
        }
      }
    } else {
      add('Provision mode', 'Single');
      var singleDisk = estimateRequestedDiskGb(form);
      if (singleDisk > 0) add('Requested disks (info)', singleDisk + ' GB');
    }
    var remote = form.querySelector('#remote_id, [name="remote_id"]');
    if (remote && remote.value) add('Remote', remote.value);
    var cores = form.querySelector('#cores');
    if (cores) add('CPU cores', cores.value);
    var ram = form.querySelector('#ram');
    if (ram) add('RAM (MB)', ram.value);
    var bridge = form.querySelector('#bridge');
    if (bridge) add('Bridge', bridge.value);
    if (!isBulk) {
      var mode = form.querySelector('#network_mode');
      if (mode) {
        add('Network mode', mode.value);
        if (mode.value === 'static') {
          add('IP', (form.querySelector('#ip_address') || {}).value);
          add('Netmask', (form.querySelector('#netmask_cidr') || {}).value);
          add('Gateway', (form.querySelector('#gateway') || {}).value);
        }
      }
      add('DNS', (form.querySelector('#dns_servers') || {}).value);
      add('VLAN', (form.querySelector('#vlan') || {}).value);
      add('Domain profile', (form.querySelector('#domain_profile') || {}).value);
    }
    var join = form.querySelector('#join_domain_checkbox');
    if (join) add('Join domain', join.checked ? 'Yes' : 'No');
    var manageDisks = form.querySelector('#manage_disks_checkbox');
    if (manageDisks) {
      add('Configure disks', manageDisks.checked ? 'Yes' : 'No');
      if (manageDisks.checked) {
        add('Grow OS (GB)', (form.querySelector('#os_grow_to_gb') || {}).value);
        if ((form.querySelector('#ensure_pagefile_disk') || {}).checked) {
          add(
            'Pagefile disk',
            ((form.querySelector('#pagefile_size_gb') || {}).value || '') +
              'G → ' +
              ((form.querySelector('#pagefile_drive_letter') || {}).value || 'P')
          );
        }
        if ((form.querySelector('#ensure_data_disk') || {}).checked) {
          add(
            'Data disk',
            ((form.querySelector('#data_size_gb') || {}).value || '') +
              'G → ' +
              ((form.querySelector('#data_drive_letter') || {}).value || 'D')
          );
        }
      }
    }
    target.innerHTML = htmlRows.join('');
  }

  async function postJson(url, payload) {
    var response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    var body = {};
    try {
      body = await response.json();
    } catch (e) {
      body = {};
    }
    return { ok: response.ok, status: response.status, body: body };
  }

  function mapServerErrors(form, errors) {
    if (!errors || typeof errors !== 'object') return;
    Object.keys(errors).forEach(function (name) {
      var el = form.querySelector('[name="' + name + '"], #' + name);
      if (el && window.GuestOSValidate) GuestOSValidate.setInvalid(el, errors[name]);
    });
  }

  global.GuestOSForms = {
    showFormAlert: showFormAlert,
    clearFormAlert: clearFormAlert,
    applyDomainProfile: applyDomainProfile,
    wireNetworkMode: wireNetworkMode,
    wireDomainJoin: wireDomainJoin,
    wireManageDisks: wireManageDisks,
    collectSysprepPayload: collectSysprepPayload,
    collectBulkPayload: collectBulkPayload,
    parseBulkRows: parseBulkRows,
    validateBulkCsvText: validateBulkCsvText,
    fillReview: fillReview,
    estimateRequestedDiskGb: estimateRequestedDiskGb,
    estimateBulkDiskSumGb: estimateBulkDiskSumGb,
    postJson: postJson,
    mapServerErrors: mapServerErrors,
  };
})(window);
