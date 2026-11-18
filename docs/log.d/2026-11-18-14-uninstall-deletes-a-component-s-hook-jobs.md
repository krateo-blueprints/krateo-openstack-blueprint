---
type: Log
title: Uninstall deletes a component's hook Jobs (#206)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Uninstall deletes a component's hook Jobs (#206).** Since 0.3.41 every Job is a `post-install,post-upgrade` hook
  (#179), and Helm never deletes hook resources at uninstall: cinder's removal left six completed Jobs and their pods,
  and a full teardown 156 pods (#204). `hook-succeeded` can't be used, because kubernetes-entrypoint waits on dependency
  Jobs by name while the component is installed. Each of the 32 charts that render hook Jobs now includes
  helm-toolkit's `job_hook_cleanup`. It is a `pre-delete` hook, weight 100, so it runs after db-drop and the clean
  Jobs. It selects `release_group=<release>`, leaves out itself, and deletes the Jobs whose `helm.sh/hook` names
  post-install or post-upgrade, with background propagation so their pods go too. Its ServiceAccount, Role (list and
  delete on Jobs, nothing else) and RoleBinding are plain release objects, so they exist while it runs. It uses the
  chart's own ks-user (or db-init, or rabbit-init) image, which has python, and always exits 0, so a failed cleanup
  leaves Jobs behind rather than blocking the uninstall. That pre-delete hooks run under the controller was measured in
  #204 (glance-clean ran at teardown); this hook itself hasn't been run on a cluster yet. New gate
  `hack/validate-hook-cleanup.py` renders each chart at defaults and with every Job on. For every render with a hook
  Job, it requires exactly one cleanup, last among pre-delete hooks, with that RBAC and a selector matching every hook
  Job but not itself. It also runs the script against a stand-in API, with another release's Job and a non-hook Job
  next to the render's, and against a failing API. Its first CI run found four hook Jobs with no labels at all
  (cloudkitty-storage-init, ironic-manage-cleaning-network, magnum-domain-ks-user, masakari-db-sync), which no
  selector can reach. They now carry helm-toolkit's labels; their pod templates are unchanged. libvirt, memcached, openvswitch and redis render no hook Job unless
  `images.local_registry.active` turns on image-repo-sync, and have no python image, so they get no cleanup.
