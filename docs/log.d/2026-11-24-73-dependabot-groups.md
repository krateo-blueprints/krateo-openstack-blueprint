---
type: Log
title: dependabot moves all the pins to one source together
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The charts 1.4.4 bump arrived as two pull requests and neither was right (#439, #441).** Each moved one of this repository's three
  pins to `krateo-blueprints/charts`. #441 moved the publisher and left release-preflight at 1.4.3, which
  `hack/validate-prerelease-channel.py` refuses.
- **The fix:** `.github/dependabot.yml` groups the pins by source, as PUBLISHING.md rule 9 says: one pull request for
  `krateo-blueprints/charts`, one for `krateo-platformops/.github`. Dependabot replaces #439 to #443 with those two.
