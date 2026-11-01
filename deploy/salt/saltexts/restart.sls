{#- Schedules a delayed restart of the salt-minion (the job returns first, the minion restarts ~10 s later).
    Commands come from pillar so tests can inject a harmless one:
      saltexts:restart_linux_cmd, saltexts:restart_windows_cmd #}
{%- set cfg = salt['pillar.get']('saltexts', {}) %}
{%- if grains['kernel']|lower == 'windows' %}
saltexts_restart:
  cmd.run:
    - name: {{ cfg.get('restart_windows_cmd', "Start-Process powershell -WindowStyle Hidden -ArgumentList '-Command','Start-Sleep 10; Restart-Service salt-minion'")|tojson }}
    - shell: powershell
{%- else %}
saltexts_restart:
  cmd.run:
    - name: {{ cfg.get('restart_linux_cmd', 'systemd-run --on-active=10 --unit=saltexts-restart systemctl restart salt-minion')|tojson }}
{%- endif %}
