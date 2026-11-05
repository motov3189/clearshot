# Clearshot maintenance

- Keep screenshot/edit/export coordinates in original image pixels. Preview or desktop scaling must not reduce exported resolution.
- Store personal preferences under the user's XDG configuration directory, outside this repository. Never commit personal settings or real screenshots; `docs/editor.png` is a generated demo only.
- At the end of every program change, update README and CHANGELOG with the resulting behavior, configuration and validation, then commit the task's files and push to the configured GitHub remote. This workflow is explicitly authorized by the owner. Preserve repository visibility and avoid unrelated changes or force pushes.
- Run `/usr/bin/python3 -m unittest discover -s tests -v` and relevant GTK smoke checks under Xvfb. Use an isolated settings backend for shortcut mutation tests; never change the real desktop shortcut as a test.
- If synchronization fails, report the concrete failure and retain the local commit; do not claim the remote is current.
