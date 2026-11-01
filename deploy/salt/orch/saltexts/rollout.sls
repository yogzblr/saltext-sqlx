{#- SKETCH, untested. Runs on the master:  salt-run state.orchestrate orch.saltexts.rollout pillar='{"tgt":"G@role:canary","batch_size":5}'
    Per batch: install/update -> (only if something changed) restart + wait for the minion start event -> verify. #}
{%- set tgt  = pillar.get('tgt', 'G@saltexts:managed') %}
{%- set size = pillar.get('batch_size', 10) %}
{%- set up   = salt['saltutil.runner']('manage.up', tgt=tgt, tgt_type='compound') %}

{%- for chunk in up|batch(size) %}
{%- set n   = loop.index %}
{%- set ids = chunk|map('string')|list %}
{%- set L   = 'L@' ~ ids|join(',') %}

apply_{{ n }}:
  salt.state:
    - tgt: {{ L }}
    - tgt_type: compound
    - sls: saltexts
    - failhard: True
    {%- if not loop.first %}
    - require: [{salt: verify_{{ n - 1 }}}]
    {%- endif %}

restart_linux_{{ n }}:
  salt.function:
    - tgt: '{{ L }} and G@kernel:Linux'
    - tgt_type: compound
    - name: cmd.run
    - arg: ['systemd-run --on-active=10 --unit=saltexts-restart systemctl restart salt-minion']
    - parallel: True
    - onchanges: [{salt: apply_{{ n }}}]

restart_windows_{{ n }}:
  salt.function:
    - tgt: '{{ L }} and G@kernel:Windows'
    - tgt_type: compound
    - name: cmd.run
    - arg: ["Start-Process powershell -WindowStyle Hidden -ArgumentList '-Command','Start-Sleep 10; Restart-Service salt-minion'"]
    - kwarg: {shell: powershell}
    - parallel: True
    - onchanges: [{salt: apply_{{ n }}}]

wait_{{ n }}:
  salt.wait_for_event:
    - name: salt/minion/*/start
    - id_list: {{ ids|json }}
    - timeout: 600
    - parallel: True
    - onchanges: [{salt: apply_{{ n }}}]

verify_{{ n }}:
  salt.state:
    - tgt: {{ L }}
    - tgt_type: compound
    - sls: saltexts.verify
    - failhard: True
    - retry: {attempts: 12, interval: 10, until: True}
    - require: [{salt: apply_{{ n }}}, {salt: wait_{{ n }}}, {salt: restart_linux_{{ n }}}, {salt: restart_windows_{{ n }}}]
{%- endfor %}
