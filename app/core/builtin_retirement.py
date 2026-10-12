"""Frozen evidence for retiring ONLY unchanged legacy starter roles.

Full owner-state SHA-256s cover every distinct General Purpose / Frontend
Designer definition in git history through c63dcd9fb (defaults revisions 1
and 2 and all pre-record releases), projected through the v71 subagent fold.
Each also has the exact v82 tool-access migration variant. Source revisions
are recorded beside the fingerprints. Do not derive these from today's
starter templates: editing a template must never turn owner data into defaults.

Unknown fields, malformed metadata, future defaults, and unmatched states
are owner data. A text-only defaults record is NOT proof about model/tools
or any other setting. Permission rules are checked separately under the DB
transaction before deleting a matching row.
"""
import hashlib
import json


SHIPPED_AGENT_STATES = {
    "general-purpose": frozenset({
        "6455f398bd4d0f91698815d2ef859aa8d1b22458ad51dea3d5a983b4493e838a",  # c63dcd9fb
        "d4855ddf36884759fbef1405c54a1044a4258888f34149017185fee34a4168ec",  # c63dcd9fb; v82
        "c50ddd44faddb263b1e3e505474341457da8e2aed27effa6d407694034bbd480",  # 93a3c948c
        "b5f5bdba5ef455728a4f761965b4b5e5cb3a07e576963a233093d56ca0a5fd7c",  # 93a3c948c; v82
        "7f690159c12da287b68c83fcc14812bfa48c37f60f8777aa6473f30241cb76a2",  # e85ae57f2
        "4ec61ef44561230c6779787846db5a4ce3febfbbf4baa148062e729938a7c50c",  # e85ae57f2; v82
        "67645656859f3aabc6d56b78cb7a7b7d6ac686bb406d54a486a206cd98a6c6e4",  # 4870adeab
        "30baea02f9e06ae1427a608b893b40cc8da3c15d41b899da796be6935e6fc3f2",  # 4870adeab; v82
        "a9fbdc08cd29844b846b48a715a74a8ed07199d997cb2b709a842692dc9a07b5",  # 99d3aeea7
        "383e72f2622af2ea78253a7a8ed6ff2266b974b39cddc912ab5ec2c4e229221a",  # 99d3aeea7; v82
        "840ea0aa716051c5807c0a103e7002e405593951eb89b39da96d26fcb3ae5011",  # 4f011b703
        "7b270faa2c6bcea3d85ef55825d3552333dfe2f326196436934f3b05e720c8b7",  # 4f011b703; v82
        "d8d3c49be63c1c22e036f57e149cac43dcc5758f0558be2755d8181ebedabc65",  # e118eec68
        "6f46f2f324c477b6545ab7a31c4ba30a73c2cbb34da2de606b4d973b7bf89553",  # e118eec68; v82
    }),
    "frontend-designer": frozenset({
        "33aac41379def53797a67dccf1248efc2f4e517da6069490b4bb92e39b698e13",  # c63dcd9fb
        "5f27f23ed3f2f26971f2431589c43ca5992cf395d292f706fe44eb122fd72c66",  # c63dcd9fb; v82
        "f3b786cbe7f75e74626f17c69a3f173014e5ebd8fe99d6fe78d9381c30c41475",  # e85ae57f2
        "3ae45416782631f426e6c034de6f923204596c12fce310169b332542296e81e6",  # e85ae57f2; v82
        "d5ab6f29b0a1d2f0fc094f71b7a1c2bd5a4ee647174398558a8ff929b932cb89",  # 4870adeab
        "7f64cde2e9abce19b61a6100efa908e4757dbe5e1b344b69d52eae71bdbe47f4",  # 4870adeab; v82
        "cb46ae1fc0bce74917c0e9240678d03adbdfcda80a955922f72842460a5690bf",  # 6f720630b
        "78204d2a4432ab729ffd9103205d2269f755d5137dcccc7cafa8d3a3e2009c0c",  # 6f720630b; v82
        "8fee0c6c73e1711f130f720ae1a24eae80a77af8ff9633269163c403eb79726d",  # 85b311885
        "be15bde7fe6697515c251e6293a3b75ad6134884149825817b03defb8ea3db0c",  # 85b311885; v82
        "f8f5479343b48e851077f1776848d8cbf87323b3b0b47f6144612b0f3c513528",  # a35139f4f
        "1252dddb6d73964b9d7c60b035ae0e7bc7523dc483a60bb94c030ea9a0a7d6e2",  # a35139f4f; v82
        "13cba9aa2a5efdb7c219adcad38002cd679a68c21fa855cd50334eba5b283159",  # e118eec68
        "d89786480630c1d3c8bf6e0413f6f97bb13e8ca2003003b201fef53235de453c",  # e118eec68; v82
    }),
}


def is_untouched_builtin(agent: dict) -> bool:
    """Match a complete known shipped state, without changing the input row."""
    known = SHIPPED_AGENT_STATES.get(agent["id"])
    if not known:
        return False
    try:
        state = {key: value for key, value in agent.items()
                 if key not in {"id", "is_builtin", "created_at", "updated_at"}}
        config = state["config"]
        config = json.loads(config) if isinstance(config, str) else dict(config)
        if "builtin_defaults" in config:
            record = config.pop("builtin_defaults")
            # Records only tracked TEXT. A missing/mismatching field means it
            # was customized, even if its new text matches an older release.
            if (not isinstance(record, dict) or set(record) != {"revision", "sha256"}
                    or type(record["revision"]) is not int or record["revision"] not in (1, 2)):
                return False
            text = {"name": state["name"], "description": state["description"],
                    "prompt": config["system_prompt"]["content"]}
            if record["sha256"] != {
                key: hashlib.sha256((value or "").encode()).hexdigest() for key, value in text.items()
            }:
                return False
        state["config"] = config
        if isinstance(state.get("mcp_tools"), str):
            state["mcp_tools"] = json.loads(state["mcp_tools"])
        fingerprint = hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return fingerprint in known
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
