"""Derive benchmark scenario ground truth from documented formulas.

Ground truth for the correctness metric must be defensible: every
asserted value has to trace back to a published tuning rule or security
benchmark rather than to the author's judgement. This module encodes
those rules once and applies them uniformly to every scenario, so the
benchmark can be regenerated if a rule changes and the derivation can be
quoted directly in the methodology chapter.

Sources
-------
- **pgtune** (https://pgtune.leopard.in.ua/) and the PostgreSQL manual,
  "Resource Consumption" chapter: ``shared_buffers`` at ~25% of RAM,
  ``effective_cache_size`` at ~75%, ``maintenance_work_mem`` at RAM/16.
- **PostgreSQL manual, "Connections and Authentication"**, plus the
  pgbouncer FAQ on connection counts exceeding core count.
- **CIS PostgreSQL / nginx / Redis Benchmarks, Level 1**: authentication,
  logging and information-disclosure controls.
- **Mozilla SSL Configuration Generator**, intermediate profile: TLS 1.2
  as the minimum protocol version.
- **Redis key-eviction documentation**: policy selection by workload.
- **PCI DSS v4.0 §4.2.1** and **UK GDPR Article 32** for the
  transit-encryption and audit-logging expectations attached to the
  stricter compliance profiles.

Run ``python -m benchmark.derive_scenarios`` to regenerate
``benchmark/scenarios/*.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

KB = 1024
MB = 1024**2
GB = 1024**3

SCENARIO_DIR = Path(__file__).parent / "scenarios"

#: Largest host a scenario may claim, in GB.
#:
#: The validator deploys each generated configuration to a real Docker
#: daemon, which on the experiment machine has 8 GB. A scenario claiming
#: more leads the agent to size ``shared_buffers`` beyond what PostgreSQL
#: can allocate; the container then exits during startup and the agent
#: misreads the failure as a configuration fault, exhausting its budget on
#: a problem that does not exist. The hardware envelope is therefore
#: bounded by the test machine, and that bound is a stated limitation of
#: the study rather than a design choice.
MAX_DEPLOYABLE_RAM_GB = 8


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One benchmark task, before ground truth is derived.

    The fields here are exactly the facts stated in the natural-language
    request, so the derivation cannot smuggle in information the agent
    was never given.
    """

    id: str
    description: str
    request: str
    workload_class: str
    compliance: str
    ram_gb: float
    vcpu: int
    concurrent_users: int
    data_size_gb: float
    backup_required: bool = False
    #: Free-text note on why this scenario is in the set. Recorded in the
    #: YAML so the sampling rationale survives into the write-up.
    rationale: str = ""
    #: Output filename stem. Defaults to :attr:`id`; set explicitly where
    #: an existing path is referenced elsewhere in the repository.
    filename: str = ""

    @property
    def stem(self) -> str:
        return self.filename or self.id


SCENARIOS: list[Scenario] = [
    Scenario(
        id="smoke_test_001",
        description="Small dev environment on constrained hardware, no compliance",
        request=(
            "I need a small dev web stack for an internal tool. Approximately 5 "
            "concurrent\ndevelopers will use it. The Postgres database will hold "
            "about 5 GB of test data.\nWe're running on a single host with 4 GB "
            "RAM and 2 vCPU. No production traffic,\nno compliance constraints, "
            "but use sane security defaults (TLS where appropriate,\nno default "
            "passwords).\n"
        ),
        workload_class="BALANCED",
        compliance="NONE",
        ram_gb=4,
        vcpu=2,
        concurrent_users=5,
        data_size_gb=5,
        rationale="Baseline smoke scenario; mid-range hardware envelope.",
        filename="smoke_test",
    ),
    Scenario(
        id="scenario_002_oltp_small",
        description="Small production API, transactional workload, no compliance",
        request=(
            "We're deploying the backend for a customer-facing booking API. Expect "
            "around\n25 concurrent users at peak, mostly short read-write "
            "transactions. The database\nwill hold roughly 10 GB. The host has 4 "
            "GB RAM and 2 vCPU. This is real\nproduction traffic so it needs to be "
            "secured properly, but we're not under any\nspecific regulatory "
            "regime.\n"
        ),
        workload_class="OLTP",
        compliance="NONE",
        ram_gb=4,
        vcpu=2,
        concurrent_users=25,
        data_size_gb=10,
        backup_required=True,
        rationale="Entry-level production OLTP; isolates workload class from compliance.",
    ),
    Scenario(
        id="scenario_003_oltp_gdpr",
        description="EU customer data platform, transactional, UK GDPR",
        request=(
            "We run a SaaS platform for UK and EU customers and need the data tier "
            "for it.\nAround 60 concurrent users, transactional access patterns, "
            "about 40 GB of\ncustomer records. Host is 8 GB RAM, 4 vCPU. We hold "
            "personal data on EU\nresidents, so we're subject to UK GDPR. We need "
            "encryption in transit and\nenough audit logging to demonstrate access "
            "control. Nightly backups required.\n"
        ),
        workload_class="OLTP",
        compliance="GDPR_UK",
        ram_gb=8,
        vcpu=4,
        concurrent_users=60,
        data_size_gb=40,
        backup_required=True,
        rationale="Same workload class as 002 with a compliance profile added.",
    ),
    Scenario(
        id="scenario_004_olap_large",
        description="Analytics warehouse on the largest available host, no compliance",
        request=(
            "I need a reporting warehouse for our internal BI team. Only about 10 "
            "analysts\nuse it, but their queries are large aggregations scanning "
            "most of a 120 GB\ndataset. The box is 8 GB RAM and 4 vCPU. Nothing "
            "regulated, it's all internal\noperational data. Batch loads run "
            "overnight.\n"
        ),
        workload_class="OLAP",
        compliance="NONE",
        ram_gb=8,
        vcpu=4,
        concurrent_users=10,
        data_size_gb=120,
        backup_required=True,
        rationale="Analytical extreme: few connections, working set far exceeding RAM.",
    ),
    Scenario(
        id="scenario_005_cache_heavy",
        description="Session and cache dominated workload, no compliance",
        request=(
            "We're standing up the tier behind a high-traffic content site. Roughly "
            "200\nconcurrent users. Most reads should be served from cache; "
            "Postgres holds only\nabout 8 GB of canonical content and gets "
            "relatively little traffic. Host is\n6 GB RAM, 4 vCPU. Cache hit rate "
            "matters more than durability for the cached\ndata itself. Standard "
            "security hygiene please.\n"
        ),
        workload_class="CACHING_HEAVY",
        compliance="NONE",
        ram_gb=6,
        vcpu=4,
        concurrent_users=200,
        data_size_gb=8,
        rationale="Cache-dominant; shifts the memory split away from Postgres.",
    ),
    Scenario(
        id="scenario_006_balanced_hipaa",
        description="Clinical records system, mixed workload, HIPAA",
        request=(
            "This is the data tier for a clinical records application used by about "
            "30\nclinicians concurrently. Mixed read and write, roughly 30 GB of "
            "patient\nrecords. Host has 6 GB RAM and 4 vCPU. We handle protected "
            "health\ninformation and must satisfy HIPAA: encryption in transit, "
            "strong\nauthentication, and audit logging of access. Backups are "
            "mandatory.\n"
        ),
        workload_class="BALANCED",
        compliance="HIPAA",
        ram_gb=6,
        vcpu=4,
        concurrent_users=30,
        data_size_gb=30,
        backup_required=True,
        rationale="Strictest logging profile at a moderate hardware envelope.",
    ),
    Scenario(
        id="scenario_007_oltp_pci",
        description="Payment processing, high concurrency, PCI DSS",
        request=(
            "We need the data tier for a payment processing service. Around 100 "
            "concurrent\nconnections, short transactions, about 25 GB of "
            "transaction history. Host is\n8 GB RAM with 4 vCPU. We are in PCI "
            "DSS scope, so cardholder data must be\nencrypted in transit with "
            "current TLS, no default credentials anywhere, and\naccess must be "
            "logged. Backups required.\n"
        ),
        workload_class="OLTP",
        compliance="PCI_DSS",
        ram_gb=8,
        vcpu=4,
        concurrent_users=100,
        data_size_gb=25,
        backup_required=True,
        rationale="Highest transactional concurrency under the strictest profile.",
    ),
    Scenario(
        id="scenario_008_olap_gdpr",
        description="EU analytics estate, analytical workload, UK GDPR",
        request=(
            "Reporting estate over EU customer behaviour data. About 8 analysts, "
            "very\nlarge scans over roughly 150 GB. Host is 8 GB RAM, 4 vCPU. The "
            "dataset\nincludes personal data on EU residents so UK GDPR applies: we "
            "need transit\nencryption and access logging. Loads are batch, "
            "overnight.\n"
        ),
        workload_class="OLAP",
        compliance="GDPR_UK",
        ram_gb=8,
        vcpu=4,
        concurrent_users=8,
        data_size_gb=150,
        backup_required=True,
        rationale="Crosses the analytical extreme with a compliance profile.",
    ),
    Scenario(
        id="scenario_009_cache_constrained",
        description="Cache-heavy workload on deliberately tight hardware",
        request=(
            "High-read internal portal, about 80 concurrent users, but we only "
            "have a\nvery small box available: 2 GB RAM and 2 vCPU. Postgres holds "
            "around 3 GB.\nWe want aggressive caching to keep the database load "
            "down. Nothing\nregulated. Be realistic about what fits in the memory "
            "available.\n"
        ),
        workload_class="CACHING_HEAVY",
        compliance="NONE",
        ram_gb=2,
        vcpu=2,
        concurrent_users=80,
        data_size_gb=3,
        rationale=(
            "Tension case: concurrency and cache ambitions exceed what the host "
            "comfortably holds, so the memory budget must be split carefully."
        ),
    ),
    Scenario(
        id="scenario_010_balanced_large",
        description="Mixed workload with the most headroom available, no compliance",
        request=(
            "General purpose application data tier, roughly 50 concurrent users "
            "and a\nmix of reads and writes over about 60 GB. We have the larger "
            "host available:\n8 GB RAM and 4 vCPU. No regulatory constraints, but "
            "follow good security\npractice. Backups nightly.\n"
        ),
        workload_class="BALANCED",
        compliance="NONE",
        ram_gb=8,
        vcpu=4,
        concurrent_users=50,
        data_size_gb=60,
        backup_required=True,
        rationale="Largest hardware envelope; tests whether tuning scales with RAM.",
    ),
    Scenario(
        id="scenario_011_oltp_hipaa_constrained",
        description="Transactional HIPAA workload on constrained hardware",
        request=(
            "Small clinical scheduling system, about 40 concurrent users, 15 GB of "
            "data.\nThe only host we have is 2 GB RAM with 2 vCPU. It still "
            "handles protected\nhealth information so HIPAA applies in full: "
            "encryption in transit, strong\npassword hashing, and audit logging of "
            "connections. Backups required.\n"
        ),
        workload_class="OLTP",
        compliance="HIPAA",
        ram_gb=2,
        vcpu=2,
        concurrent_users=40,
        data_size_gb=15,
        backup_required=True,
        rationale=(
            "Tension case: strict compliance overhead on hardware that cannot "
            "absorb generous connection or memory settings."
        ),
    ),
    Scenario(
        id="scenario_012_olap_pci",
        description="Reporting over cardholder data, analytical, PCI DSS",
        request=(
            "Reporting database over historical card transaction data. About 15 "
            "analysts\nrunning large aggregate queries across roughly 100 GB. Host "
            "is 6 GB RAM,\n4 vCPU. This is in PCI DSS scope: current TLS in "
            "transit, no default\ncredentials, access logging. Batch loads "
            "overnight, backups required.\n"
        ),
        workload_class="OLAP",
        compliance="PCI_DSS",
        ram_gb=6,
        vcpu=4,
        concurrent_users=15,
        data_size_gb=100,
        backup_required=True,
        rationale="Completes the workload x compliance coverage grid.",
    ),
]


# ---------------------------------------------------------------------------
# Size formatting
# ---------------------------------------------------------------------------


def format_size(num_bytes: float) -> str:
    """Render a byte count as a schema-valid memory string.

    Output matches ``MEMORY_PATTERN`` in ``src.schemas.postgres`` (an
    integer or one-decimal magnitude followed by MB or GB), so generated
    bounds can be fed straight back through ``parse_size``.
    """
    if num_bytes >= GB:
        value = round(num_bytes / GB, 1)
        unit = "GB"
    else:
        value = round(num_bytes / MB)
        unit = "MB"
    if float(value) == int(value):
        return f"{int(value)}{unit}"
    return f"{value}{unit}"


# ---------------------------------------------------------------------------
# Derivation rules
# ---------------------------------------------------------------------------

#: Fraction of host RAM allocated to ``shared_buffers``.
#:
#: pgtune and the PostgreSQL manual both target 25% of RAM. The band is
#: widened here because all three services share one host in this study,
#: so a deliberately conservative Postgres allocation is a legitimate
#: answer rather than an error. The upper bound stays below the ~40%
#: point past which the manual notes returns diminish.
SHARED_BUFFERS_FRACTION = (0.15, 0.35)

#: ``effective_cache_size`` is a planner hint, not an allocation. pgtune
#: uses 75% of RAM on a dedicated host; the lower bound here reflects the
#: memory that nginx and Redis also occupy.
EFFECTIVE_CACHE_FRACTION = (0.40, 0.75)

#: ``maintenance_work_mem``: pgtune uses RAM/16, capped at 2GB.
MAINTENANCE_WORK_MEM_FRACTION = (1 / 32, 1 / 8)
MAINTENANCE_WORK_MEM_CAP = 2 * GB

#: ``max_connections`` baseline by workload, taken from pgtune's DB-type
#: table (web/oltp 200-300, mixed 100, dw 40). pgtune does not scale this
#: figure by core count, so neither does this derivation.
CONNECTIONS_BASE: dict[str, int] = {
    "OLTP": 200,
    "BALANCED": 100,
    "CACHING_HEAVY": 100,
    "OLAP": 40,
}

#: Share of stated concurrent users whose requests reach PostgreSQL.
#:
#: Scenario requests state total system concurrency, but not every user
#: opens a database connection: a cache-first design absorbs most reads
#: in Redis, while analysts in an OLAP estate all query the database
#: directly. These factors are a modelling assumption of this study and
#: are reported as such in the methodology chapter.
POSTGRES_CONCURRENCY_SHARE: dict[str, float] = {
    "OLTP": 1.0,
    "OLAP": 1.0,
    "BALANCED": 0.6,
    "CACHING_HEAVY": 0.2,
}

#: The stack contains no connection pooler, so ``max_connections`` must
#: at minimum serve stated peak database concurrency with 25% headroom
#: for idle-in-transaction sessions. The upper bound is the pgtune
#: baseline or four times database concurrency, whichever is larger.
#:
#: The lower bound is a hard feasibility constraint: below it the stated
#: workload cannot connect. The upper bound is deliberately permissive,
#: because scoring over-provisioning as incorrect would need a threshold
#: derived from ``work_mem`` times connections against host RAM, and this
#: study does not assert ``work_mem``. Over-provisioning is therefore
#: reported descriptively rather than scored.
CONNECTIONS_FLOOR = 20
CONNECTIONS_LOWER_MULTIPLE = 1.25
CONNECTIONS_UPPER_MULTIPLE = 4

#: Redis ``maxmemory`` as a fraction of host RAM, by workload class.
REDIS_MEMORY_FRACTION: dict[str, tuple[float, float]] = {
    "CACHING_HEAVY": (0.15, 0.40),
    "BALANCED": (0.05, 0.20),
    "OLTP": (0.05, 0.20),
    "OLAP": (0.02, 0.15),
}

#: Acceptable eviction policies by workload (Redis key-eviction docs).
#: A pure cache should evict; a store of record should not silently drop
#: keys, so ``noeviction`` stays acceptable outside cache-first workloads.
REDIS_POLICIES: dict[str, list[str]] = {
    "CACHING_HEAVY": ["allkeys-lru", "allkeys-lfu"],
    "BALANCED": ["allkeys-lru", "allkeys-lfu", "noeviction"],
    "OLTP": ["allkeys-lru", "allkeys-lfu", "volatile-lru", "noeviction"],
    "OLAP": ["allkeys-lru", "allkeys-lfu", "noeviction"],
}

#: Minimum CIS Level 1 pass rate expected per compliance profile.
#:
#: Compared against ``cis_pass_rate_actionable`` — the 26 of 32 controls a
#: specification can actually satisfy — not against the raw rate. Six
#: controls are unreachable through the agent-facing schema (see
#: ``src.validator.cis_checks.UNREACHABLE_CONTROLS``), which capped the raw
#: rate at 26/32 = 0.813 and so put the GDPR and HIPAA/PCI floors below the
#: achievable maximum by construction: every compliance scenario failed its
#: floor no matter how the agent behaved.
#:
#: This is the same fault as the withdrawn vCPU-scaled ``max_connections``
#: rule — a threshold that no run could meet measures the rule, not the
#: agent — and is corrected the same way, by fixing the rule rather than
#: reinterpreting the results.
#:
#: The values themselves are unchanged; only the denominator they are
#: compared against has been corrected. Against 26 actionable controls the
#: pilot's typical 24 passes give 0.923, so all three floors now sit below
#: the observed level and can discriminate in either direction.
CIS_MINIMUM: dict[str, float] = {
    "NONE": 0.75,
    "GDPR_UK": 0.85,
    "HIPAA": 0.90,
    "PCI_DSS": 0.90,
}

#: Profiles that mandate demonstrable access logging (UK GDPR Art. 32;
#: HIPAA Security Rule audit controls; PCI DSS v4.0 Req. 10).
AUDIT_LOGGING_PROFILES = {"GDPR_UK", "HIPAA", "PCI_DSS"}

#: Profiles that mandate statement-level logging beyond connections.
STATEMENT_LOGGING_PROFILES = {"HIPAA", "PCI_DSS"}


def derive_ground_truth(s: Scenario) -> dict[str, Any]:
    """Apply the derivation rules to one scenario.

    Every emitted key is one the correctness scorer's mapping table can
    resolve; the generator validates this before writing.
    """
    ram = s.ram_gb * GB

    # --- PostgreSQL -----------------------------------------------------
    sb_lo, sb_hi = SHARED_BUFFERS_FRACTION
    ec_lo, ec_hi = EFFECTIVE_CACHE_FRACTION
    mw_lo, mw_hi = MAINTENANCE_WORK_MEM_FRACTION

    pg_users = max(
        1, round(s.concurrent_users * POSTGRES_CONCURRENCY_SHARE[s.workload_class])
    )
    conn_lo = max(CONNECTIONS_FLOOR, round(CONNECTIONS_LOWER_MULTIPLE * pg_users))
    conn_hi = max(
        CONNECTIONS_BASE[s.workload_class], CONNECTIONS_UPPER_MULTIPLE * pg_users
    )

    postgres: dict[str, Any] = {
        "shared_buffers_range": [format_size(ram * sb_lo), format_size(ram * sb_hi)],
        "effective_cache_size_range": [
            format_size(ram * ec_lo),
            format_size(ram * ec_hi),
        ],
        "maintenance_work_mem_range": [
            format_size(min(ram * mw_lo, MAINTENANCE_WORK_MEM_CAP)),
            format_size(min(ram * mw_hi, MAINTENANCE_WORK_MEM_CAP)),
        ],
        "max_connections_range": [conn_lo, conn_hi],
        "ssl": True,
        "password_encryption": "scram-sha-256",
    }
    if s.backup_required:
        # Point-in-time recovery needs at least replica-level WAL.
        postgres["wal_level_acceptable"] = ["replica", "logical"]
    if s.compliance in AUDIT_LOGGING_PROFILES:
        postgres["log_connections"] = True
        postgres["log_disconnections"] = True
    if s.compliance in STATEMENT_LOGGING_PROFILES:
        postgres["log_statement_acceptable"] = ["ddl", "mod", "all"]

    # --- nginx ----------------------------------------------------------
    nginx: dict[str, Any] = {
        "server_tokens": False,
        "ssl_protocols_min": "TLSv1.2",
    }
    if s.compliance in AUDIT_LOGGING_PROFILES:
        nginx["autoindex"] = False

    # --- Redis ----------------------------------------------------------
    rm_lo, rm_hi = REDIS_MEMORY_FRACTION[s.workload_class]
    redis: dict[str, Any] = {
        "maxmemory_set": True,
        "maxmemory_range": [format_size(ram * rm_lo), format_size(ram * rm_hi)],
        "maxmemory_policy_acceptable": REDIS_POLICIES[s.workload_class],
        "requirepass_or_acl": True,
        "protected_mode": True,
    }

    return {
        "workload_class": s.workload_class,
        "compliance": s.compliance,
        "expected_postgres": postgres,
        "expected_nginx": nginx,
        "expected_redis": redis,
        "expected_cis_minimum_pass_rate": CIS_MINIMUM[s.compliance],
    }


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def build_document(s: Scenario) -> dict[str, Any]:
    """Assemble the full scenario YAML document."""
    return {
        "id": s.id,
        "description": s.description,
        "request": s.request,
        "derivation": {
            "rationale": s.rationale,
            "stated_facts": {
                "ram_gb": s.ram_gb,
                "vcpu": s.vcpu,
                "concurrent_users": s.concurrent_users,
                "data_size_gb": s.data_size_gb,
                "backup_required": s.backup_required,
            },
            "note": (
                "Ground truth is generated by benchmark/derive_scenarios.py. "
                "Edit the rules there, not this file."
            ),
        },
        "ground_truth": derive_ground_truth(s),
    }


def write_all(out_dir: Path = SCENARIO_DIR) -> list[Path]:
    """Regenerate every scenario YAML. Returns the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for s in SCENARIOS:
        doc = build_document(s)
        path = out_dir / f"{s.stem}.yaml"
        path.write_text(
            "# Generated by benchmark/derive_scenarios.py - do not edit by hand.\n"
            + yaml.dump(doc, sort_keys=False, default_flow_style=False, width=88),
            encoding="utf-8",
        )
        written.append(path)
    return written


if __name__ == "__main__":
    paths = write_all()
    print(f"Wrote {len(paths)} scenarios to {SCENARIO_DIR}")
    for p in paths:
        print(f"  {p.name}")
