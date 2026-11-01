{#- Runs on the master:
        salt-run state.orchestrate orch.saltexts.rollout pillar='{"tgt":"G@role:canary","batch_size":5}'

    Per batch (batches run strictly one after the other; a failed batch stops the whole run):
        stage_N    copy wheels + pins + helper to the minions                     (saltexts.stage)
        apply_N    pip install what the pins say                                  (saltexts.apply)
        restart_N  ONLY if apply_N changed something: schedule the minion restart (saltexts.restart)
        wait_N     ONLY if apply_N changed something: wait for salt/minion/<id>/start of every minion of the batch
        verify_N   the expected modules are loaded in the running minion; retried while the minions reconnect

    Orchestration pillar (all optional):
        tgt             compound target, default G@saltexts:managed
        batch_size      minions per batch, default 10
        wait_timeout    seconds to wait for the start events, default 600
        verify_attempts / verify_interval   default 12 x 10 s
        sls             state to apply, default saltexts (stage+apply). Do not change.
    The restart commands are MINION pillar: saltexts:restart_linux_cmd / saltexts:restart_windows_cmd (see saltexts/restart.sls).

    Everything uses failhard: the first failing step ends the run, so no later batch is touched.
    NOTE `onchanges` on salt.state is true only when a minion's state run reported changes. That is why `apply` is a sls of its own:
    copying a wheel (saltexts.stage) must not restart anybody. #}
{%- set tgt     = pillar.get('tgt', 'G@saltexts:managed') %}
{%- set size    = pillar.get('batch_size', 10)|int %}
{%- set wait_to = pillar.get('wait_timeout', 600)|int %}
{%- set v_att   = pillar.get('verify_attempts', 12)|int %}
{%- set v_int   = pillar.get('verify_interval', 10)|int %}
{%- set up      = salt.saltutil.runner('manage.up', tgt=tgt, tgt_type='compound')|sort %}

{%- if not up %}
saltexts_no_minions_up:
  test.configurable_test_state:
    - result: False
    - changes: False
    - comment: {{ ("no minion matching '" ~ tgt ~ "' answered manage.up: refusing to report success")|tojson }}
{%- endif %}

{%- for chunk in up|batch(size) %}
{%- set n   = loop.index %}
{%- set ids = chunk|map('string')|list %}
{%- set L   = 'L@' ~ ids|join(',') %}

stage_{{ n }}:
  salt.state:
    - tgt: {{ L|tojson }}
    - tgt_type: compound
    - sls: saltexts.stage
    - failhard: True
    {%- if not loop.first %}
    - require: [{salt: verify_{{ n - 1 }}}]
    {%- endif %}

apply_{{ n }}:
  salt.state:
    - tgt: {{ L|tojson }}
    - tgt_type: compound
    - sls: saltexts.apply
    - failhard: True
    - require: [{salt: stage_{{ n }}}]

restart_{{ n }}:
  salt.state:
    - tgt: {{ L|tojson }}
    - tgt_type: compound
    - sls: saltexts.restart
    - failhard: True
    - parallel: True
    - onchanges: [{salt: apply_{{ n }}}]

wait_{{ n }}:
  salt.wait_for_event:
    - name: salt/minion/*/start
    - id_list: {{ ids|json }}
    - timeout: {{ wait_to }}
    - failhard: True
    - parallel: True
    - onchanges: [{salt: apply_{{ n }}}]

verify_{{ n }}:
  salt.state:
    - tgt: {{ L|tojson }}
    - tgt_type: compound
    - sls: saltexts.verify
    - failhard: True
    - retry: {attempts: {{ v_att }}, interval: {{ v_int }}, until: True}
    - require: [{salt: apply_{{ n }}}, {salt: restart_{{ n }}}, {salt: wait_{{ n }}}]
{%- endfor %}
