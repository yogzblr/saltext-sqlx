{#- Apply: install what the pins say, from the staged wheelhouse. The ONLY state here is the install, so an orchestration
    `onchanges` on this sls means "a package was installed" (and not "a wheel file was copied"). Needs saltexts.stage to have run
    first (init.sls orders them; the orchestration runs stage and apply as two steps). #}
{%- from "saltexts/map.jinja" import m with context %}
{%- if not m.guard %}
saltexts_apply:
  cmd.run:
    - name: {{ m.run_cmd|tojson }}
    - unless: {{ m.check_cmd|tojson }}
    {%- if m.win %}
    - shell: powershell
    {%- endif %}
    - stateful: True
{%- endif %}
