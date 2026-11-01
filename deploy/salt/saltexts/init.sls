{#- Installs/updates the pinned saltext wheels from the master file server. Install only: the orchestration
    (orch/saltexts/rollout.sls) owns restarts.

    Pillar (see deploy/pillar/saltexts.sls.example):
      saltexts:python        Python (major.minor) the wheels were built for; must equal the minion's Salt Python
      saltexts:packages      {"saltext-sqlx[postgres]": "0.1.0", ...}  the one place to bump a version
      saltexts:wheels_root   default "saltexts/wheels"  (salt://<wheels_root>/<kernel>-<arch>)
      saltexts:pip_target    "" (default: plain pip into the running Python) | "auto" | a directory (salt-pip style install)

    Layout on the minion (cachedir = minion cache dir):
      <cachedir>/saltext-wheels/   exact copy of the wheelhouse folder (file.recurse with clean: True owns this folder alone)
      <cachedir>/saltext-state/    pins.json, applied.json (marker), saltexts_apply.py  (never touched by the clean above) #}
{%- set cfg    = salt['pillar.get']('saltexts', {}) %}
{%- set pins   = cfg.get('packages', {}) %}
{%- set pyver  = cfg.get('python', '3.14')|string %}
{%- set root   = cfg.get('wheels_root', 'saltexts/wheels') %}
{%- set ptarget = cfg.get('pip_target', '') %}
{%- set cpu    = grains['cpuarch']|lower %}
{%- set arch   = 'arm64' if cpu in ('aarch64','arm64') else ('x86_64' if cpu in ('x86_64','amd64') else 'unknown') %}
{%- set os_    = grains['kernel']|lower %}
{%- set target = os_ ~ '-' ~ arch %}
{%- set win    = (os_ == 'windows') %}
{%- set sep    = '\\' if win else '/' %}
{%- set cache  = opts['cachedir'] ~ sep ~ 'saltext-wheels' %}
{%- set sdir   = opts['cachedir'] ~ sep ~ 'saltext-state' %}
{%- set py     = grains['pythonexecutable'] %}
{%- set pyhave = grains['pythonversion'][0] ~ '.' ~ grains['pythonversion'][1] %}
{%- set helper = sdir ~ sep ~ 'saltexts_apply.py' %}
{%- set pinsf  = sdir ~ sep ~ 'pins.json' %}
{%- set marker = sdir ~ sep ~ 'applied.json' %}
{#- QUOTING. The cmd.run STATE always runs with python_shell=True (Salt forces it, also for `unless`), so a shell sits between Salt and
    Python on both platforms. Every argument is passed as a SINGLE-QUOTED literal: POSIX sh and PowerShell do no expansion inside '...',
    and a single quote inside a value is escaped ('\'' for sh, '' for PowerShell). On Windows the string contains no double quote at all,
    so Salt's list2cmdline() adds no \" escaping when it hands -Command to powershell.exe. `exit $LASTEXITCODE` keeps the helper's code. #}
{%- macro q(v) -%}
  {%- if win -%}'{{ v|string|replace("'", "''") }}'{%- else -%}'{{ v|string|replace("'", "'\\''") }}'{%- endif -%}
{%- endmacro %}
{%- set argl   = q(py) ~ ' ' ~ q(helper) ~ ' ' ~ q(pinsf) ~ ' ' ~ q(cache) ~ ' ' ~ q(marker) ~ (' --target ' ~ q(ptarget) if ptarget else '') %}
{%- set run_cmd   = ('& ' ~ argl ~ '; exit $LASTEXITCODE') if win else argl %}
{%- set check_cmd = ('& ' ~ argl ~ ' --check; exit $LASTEXITCODE') if win else (argl ~ ' --check') %}

{%- if target not in ['linux-x86_64','linux-arm64','windows-x86_64','windows-arm64'] %}
saltexts_unsupported_platform:
  test.configurable_test_state:
    - result: False
    - changes: False
    - comment: {{ ("saltexts: no wheelhouse for " ~ target ~ " (supported: linux/windows x x86_64/arm64)")|tojson }}
{%- elif pyhave != pyver %}
saltexts_python_mismatch:
  test.configurable_test_state:
    - result: False
    - changes: False
    - comment: {{ ("saltexts: wheels are built for Python " ~ pyver ~ ", this minion's Salt has " ~ pyhave ~ " (fix pillar saltexts:python or rebuild the wheelhouse)")|tojson }}
{%- elif not pins %}
saltexts_no_pins:
  test.configurable_test_state:
    - result: False
    - changes: False
    - comment: {{ "saltexts: pillar saltexts:packages is empty"|tojson }}
{%- else %}
saltexts_wheels:
  file.recurse:
    - name: {{ cache }}
    - source: salt://{{ root }}/{{ target }}
    - clean: True
    - makedirs: True
    {%- if not win %}
    - dir_mode: '0755'
    - file_mode: '0644'
    {%- endif %}

saltexts_state_dir:
  file.directory:
    - name: {{ sdir }}
    - makedirs: True
    {%- if not win %}
    - mode: '0750'
    {%- endif %}

saltexts_pins:
  file.managed:
    - name: {{ pinsf }}
    - contents: {{ (pins|tojson(sort_keys=True))|yaml_encode }}
    - require: [{file: saltexts_state_dir}]

saltexts_helper:
  file.managed:
    - name: {{ helper }}
    - source: salt://saltexts/files/saltexts_apply.py
    - require: [{file: saltexts_state_dir}]

saltexts_apply:
  cmd.run:
    - name: {{ run_cmd|tojson }}
    - unless: {{ check_cmd|tojson }}
    {%- if win %}
    - shell: powershell
    {%- endif %}
    - stateful: True
    - require: [{file: saltexts_wheels}, {file: saltexts_pins}, {file: saltexts_helper}]
{%- endif %}
