"""Worker network: independent workers that share one encrypted sync set.

Package layout (see docs/network/README.md for the design as built):

- ``crypto``          primitives: Ed25519, X25519 key wrapping, AES-256-GCM, HKDF
- ``identity``        this worker's device identity under credential-key custody
- ``envelope``        the signed, encrypted operation envelope and canonical JSON
- ``membership``      network key wrapping, membership certificates, revocation
- ``policy``          which collections and system-settings keys are shared
- ``engine``          outbox / inbox / apply / conflicts / snapshot / presence
- ``transport``       peer delivery over the worker API and the sync loops
- ``tokens``          short-lived device tokens obtained by signed challenge
- ``enrollment``      pairing with the ``network:join`` purpose
- ``hub``             the account hub feed client (register, push, pull, approvals)
- ``recovery``        the recovery key that unlocks a network with no worker online
- ``runners``         the directory of network runners that replaces ``installs``
- ``catalog``         owner-ordered session catalog and transcript mirrors
- ``control``         driving a session that executes on another device
- ``ownership``       which device fires an automation or holds a connector
- ``memory_records``  facts and skills as records
- ``prose_skills``    ``~/.fluid/skills/<name>/`` prose skill folders, one record each
- ``plugins_sync``    desired plugin state
- ``blobs``           content-addressed bytes for artifacts and note attachments
- ``startup``         boot wiring, single-install migration, resource hooks
"""
