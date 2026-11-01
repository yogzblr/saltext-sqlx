{#- Installs/updates the pinned saltext wheels from the master file server. Install only: the orchestration
    (orch/saltexts/rollout.sls) owns restarts. stage = copy files, apply = pip install. See map.jinja for the pillar. #}
{%- from "saltexts/map.jinja" import m with context %}
include:
  - saltexts.stage
  - saltexts.apply
{%- if not m.guard %}

extend:
  saltexts_apply:
    cmd:
      - require:
        - file: saltexts_wheels
        - file: saltexts_pins
        - file: saltexts_helper
{%- endif %}
