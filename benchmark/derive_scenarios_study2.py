"""Derive Study 2 scenario ground truth, including service selection.

Study 2 asks a question Study 1 could not: *which* services does this
workload need? The twelve Study 1 scenarios all require the same three,
so selection there is not a decision. Here each scenario carries an
``expected_services`` list, and the per-service ground-truth blocks are
emitted **only for the services that scenario expects** — a scenario
that needs no cache asserts nothing about Redis, so an agent that
deploys one is scored on selection, not on how well it tuned something
that should not exist.

Derivation discipline is inherited from :mod:`benchmark.derive_scenarios`
and extended, not replaced:

- Every parameter rule (memory fractions, connection bands, eviction
  policies, CIS floors) is imported from that module, so the two studies
  score parameters against identical formulas.
- Selection is derived the same way: each scenario states four workload
  facts (does it keep relational data, serve HTTP, re-read the same data,
  defer work) and the expected service set is a pure function of those
  facts. The facts are not free choices — each carries the phrase from
  the request that establishes it, recorded in the YAML as
  ``selection_evidence`` and checked for completeness before writing.

If a service's inclusion or exclusion cannot be justified from the
request text alone, the scenario is badly written. Fix the request, not
the ground truth.

Run ``python -m benchmark.derive_scenarios_study2`` to regenerate
``benchmark/scenarios_study2/*.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from benchmark.derive_scenarios import (
    CIS_MINIMUM,
    GB,
    MAX_DEPLOYABLE_RAM_GB,
    Scenario,
    derive_ground_truth,
    format_size,
)

SCENARIO_DIR = Path(__file__).parent / "scenarios_study2"

#: Catalog service names, in the order they should appear in
#: ``expected_services``. Kept as a literal rather than imported from
#: ``src.services.catalog`` so the benchmark package stays free of the
#: deployment stack; ``validate_documents`` checks the two agree.
CATALOG_ORDER = ("postgres", "nginx", "redis", "rabbitmq")


# ---------------------------------------------------------------------------
# RabbitMQ derivation rules
# ---------------------------------------------------------------------------
#
# RabbitMQ has no CIS Benchmark, so these trace to the vendor's own
# documentation, cited per rule. Same standard as the other three: a
# published recommendation, not the author's taste.

#: ``vm_memory_high_watermark`` as a fraction of available RAM.
#:
#: Source: RabbitMQ "Memory Alarms". The shipped default is 0.6, and the
#: page is explicit that the value must stay below 1.0 or the alarm can
#: never fire before the kernel OOM-kills the node. The lower bound is
#: set at 0.3 because the broker shares its host with up to three other
#: services here; below that the queue has too little room to buffer.
MEMORY_WATERMARK_RANGE = (0.3, 0.6)

#: ``disk_free_limit.absolute``.
#:
#: Source: RabbitMQ "Disk Alarms". The shipped default of 50MB is
#: described as suitable only for development; the recommendation is a
#: limit on the order of the node's RAM, because a node under memory
#: pressure may page its whole backlog to disk. The band therefore runs
#: from a 500MB production floor to the documented RAM-scale figure.
#: Above that, a container whose volume is modest would sit in a
#: permanent disk alarm and refuse publishes, so the upper bound is a
#: deployability constraint as much as a tuning one.
DISK_FREE_FLOOR = 500 * (1024**2)

#: ``heartbeat`` in seconds.
#:
#: Source: RabbitMQ "Networking". The default is 60. Zero disables dead
#: peer detection entirely; the docs warn that values below roughly 5s
#: risk false positives on a loaded node. The band is deliberately wide
#: because the right value depends on intervening network devices, which
#: the scenarios do not describe.
HEARTBEAT_RANGE = (10, 120)


def derive_rabbitmq(s: Scenario) -> dict[str, Any]:
    """Ground truth for the queue, when the scenario expects one."""
    ram = s.ram_gb * GB
    wm_lo, wm_hi = MEMORY_WATERMARK_RANGE
    return {
        "vm_memory_high_watermark_range": [wm_lo, wm_hi],
        "disk_free_limit_range": [format_size(DISK_FREE_FLOOR), format_size(ram)],
        "heartbeat_range": list(HEARTBEAT_RANGE),
        # RabbitMQ Production Checklist: the shipped guest account must
        # not survive into production, and connections must be bounded.
        "default_user_not_guest": True,
        "default_pass_set": True,
        "max_connections_set": True,
        # RabbitMQ Management: the HTTP API is a full administrative
        # surface and belongs on an internal interface.
        "management_listener_ip": "127.0.0.1",
    }


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Study2Scenario:
    """A Study 1 scenario plus the four facts that determine selection.

    The workload facts are what the request states; the expected service
    set is derived from them. ``selection_evidence`` records, for every
    catalog service, the phrase in the request that justifies including
    or excluding it — written for the reader of the methodology chapter,
    and checked for completeness by :func:`validate_documents`.
    """

    base: Scenario
    needs_relational_store: bool
    needs_http_frontend: bool
    needs_repeated_reads: bool
    needs_deferred_work: bool
    selection_evidence: dict[str, str]
    #: Which of the four coverage roles this scenario fills, recorded so
    #: the spread is auditable rather than asserted.
    coverage: str = ""
    notes: str = field(default="")

    @property
    def expected_services(self) -> list[str]:
        """Derived, never hand-written."""
        implied = {
            "postgres": self.needs_relational_store,
            "nginx": self.needs_http_frontend,
            "redis": self.needs_repeated_reads,
            "rabbitmq": self.needs_deferred_work,
        }
        return [name for name in CATALOG_ORDER if implied[name]]


def _s(**kwargs: Any) -> Scenario:
    return Scenario(**kwargs)


SCENARIOS: list[Study2Scenario] = [
    # -- minimal ------------------------------------------------------------
    Study2Scenario(
        base=_s(
            id="s2_001_static_brochure",
            description="Static marketing site, no dynamic content at all",
            request=(
                "We need our company brochure site online. It's a folder of "
                "pre-built HTML,\nCSS and images that our agency hands over — "
                "there are no logins, no forms and\nnothing that changes "
                "between visitors. Traffic is light, maybe a couple of dozen\n"
                "people looking at it in a day. The assets come to about half a "
                "gigabyte. We\nhave a small 2 GB, 2 vCPU box spare. Serve it "
                "quickly and don't advertise what\nsoftware version we're "
                "running.\n"
            ),
            workload_class="BALANCED",
            compliance="NONE",
            ram_gb=2,
            vcpu=2,
            concurrent_users=5,
            data_size_gb=0.5,
            rationale="Minimal case: one service. Tests whether the agent can decline.",
        ),
        needs_relational_store=False,
        needs_http_frontend=True,
        needs_repeated_reads=False,
        needs_deferred_work=False,
        coverage="minimal",
        selection_evidence={
            "postgres": (
                "EXCLUDE: 'no logins, no forms and nothing that changes between "
                "visitors' — the content is pre-built files handed over by an "
                "agency, so there is no application state to persist."
            ),
            "nginx": (
                "INCLUDE: 'serve it quickly' a folder of pre-built HTML, CSS and "
                "images — an HTTP server is the entire requirement."
            ),
            "redis": (
                "EXCLUDE: static files are already served from the filesystem and "
                "page cache; there is no computed result to memoise, and traffic "
                "is 'a couple of dozen people in a day'."
            ),
            "rabbitmq": (
                "EXCLUDE: no work is described at all beyond serving files, so "
                "there is nothing to defer."
            ),
        },
    ),
    # -- distractors --------------------------------------------------------
    Study2Scenario(
        base=_s(
            id="s2_002_distractor_cache",
            description="Internal admin console; caching name-dropped but unwarranted",
            request=(
                "Our ops lead keeps saying we should put a caching layer in "
                "because that's\nwhat they used on their last project. I'm not "
                "convinced. This is the internal\nadmin console for our support "
                "team — about 8 of them use it, and they spend\ntheir time "
                "editing customer records rather than reading the same page over "
                "and\nover. The whole database is around 2 GB. Host is 4 GB RAM, "
                "2 vCPU. Nothing\nregulated, but it is real customer data so "
                "secure it properly.\n"
            ),
            workload_class="OLTP",
            compliance="NONE",
            ram_gb=4,
            vcpu=2,
            concurrent_users=8,
            data_size_gb=2,
            backup_required=True,
            rationale=(
                "Distractor: the request names caching, but every stated fact "
                "argues against it."
            ),
        ),
        needs_relational_store=True,
        needs_http_frontend=True,
        needs_repeated_reads=False,
        needs_deferred_work=False,
        coverage="distractor-cache",
        selection_evidence={
            "postgres": (
                "INCLUDE: 'editing customer records', 'the whole database is "
                "around 2 GB' — an existing relational store is stated outright."
            ),
            "nginx": (
                "INCLUDE: it is a web console used by a support team, so it is "
                "served over HTTP."
            ),
            "redis": (
                "EXCLUDE, despite being named. The caching layer is attributed to "
                "an opinion ('that's what they used on their last project') and "
                "contradicted by three stated facts: 8 concurrent users, a "
                "write-dominated access pattern ('editing customer records rather "
                "than reading the same page over and over'), and a 2 GB dataset "
                "that fits inside PostgreSQL's own buffer cache on a 4 GB host. "
                "There is no read amplification for a cache to absorb."
            ),
            "rabbitmq": (
                "EXCLUDE: all described work is interactive record editing; "
                "nothing is deferred or batched."
            ),
        },
    ),
    Study2Scenario(
        base=_s(
            id="s2_003_distractor_queue",
            description="Read-heavy public catalogue; queue name-dropped but unwarranted",
            request=(
                "The architecture doc we inherited mentions a message broker, "
                "but I can't see\nwhat it would do here. This is a public parts "
                "catalogue: people search it,\nopen a listing, and that's the "
                "whole interaction — everything happens while\nthey wait and "
                "there's nothing to do afterwards. It's busy though, around 150 "
                "at\nonce at peak, and the popular listings get opened "
                "constantly while the\nunderlying catalogue is only reloaded "
                "from our supplier once a night. About\n12 GB of parts data. "
                "Host is 8 GB RAM, 4 vCPU.\n"
            ),
            workload_class="CACHING_HEAVY",
            compliance="NONE",
            ram_gb=8,
            vcpu=4,
            concurrent_users=150,
            data_size_gb=12,
            rationale=(
                "Distractor: the request names a broker, but every interaction "
                "described is synchronous."
            ),
        ),
        needs_relational_store=True,
        needs_http_frontend=True,
        needs_repeated_reads=True,
        needs_deferred_work=False,
        coverage="distractor-queue",
        selection_evidence={
            "postgres": (
                "INCLUDE: 'about 12 GB of parts data' reloaded nightly — a "
                "queryable catalogue of record."
            ),
            "nginx": (
                "INCLUDE: a public site that people search and browse, at 150 "
                "concurrent users."
            ),
            "redis": (
                "INCLUDE: 'the popular listings get opened constantly while the "
                "underlying catalogue is only reloaded once a night' — repeated "
                "identical reads over data that changes daily is the textbook "
                "cache case, and it is stated even though the word cache is not "
                "used."
            ),
            "rabbitmq": (
                "EXCLUDE, despite the broker being named. Its inclusion is "
                "attributed to an inherited document the requester questions, and "
                "the request states the opposite requirement outright: "
                "'everything happens while they wait and there's nothing to do "
                "afterwards'. There is no deferred work for a queue to carry."
            ),
        },
    ),
    # -- inferred -----------------------------------------------------------
    Study2Scenario(
        base=_s(
            id="s2_004_inferred_queue",
            description="Deferred order processing; queue implied, never named",
            request=(
                "Orders come in through the website all day, but we don't take "
                "payment at the\ntime. They pile up and a run goes through them "
                "overnight to charge the card\nand book stock, then each "
                "customer gets an email the next morning telling them\nwhat "
                "happened. The website itself is straightforward — around 40 "
                "people\nordering at once at peak, roughly 20 GB of order "
                "history. Each order is\nread once by the overnight run and "
                "rarely looked at again. Host is 8 GB RAM,\n4 vCPU, and we need "
                "nightly backups.\n"
            ),
            workload_class="OLTP",
            compliance="NONE",
            ram_gb=8,
            vcpu=4,
            concurrent_users=40,
            data_size_gb=20,
            backup_required=True,
            rationale=(
                "Inference: work is explicitly deferred from request time to a "
                "later run, without the word queue appearing."
            ),
        ),
        needs_relational_store=True,
        needs_http_frontend=True,
        needs_repeated_reads=False,
        needs_deferred_work=True,
        coverage="inferred-queue",
        selection_evidence={
            "postgres": (
                "INCLUDE: 'roughly 20 GB of order history' that outlives the "
                "request that created it."
            ),
            "nginx": (
                "INCLUDE: 'orders come in through the website', 40 concurrent "
                "users at peak."
            ),
            "redis": (
                "EXCLUDE: 'each order is read once by the overnight run and "
                "rarely looked at again' — explicitly no re-read locality, so a "
                "cache would hold entries nothing asks for twice."
            ),
            "rabbitmq": (
                "INCLUDE, by inference. The word queue never appears, but the "
                "request describes its defining property twice: work accepted at "
                "request time is not performed at request time ('we don't take "
                "payment at the time... a run goes through them overnight'), and "
                "a second deferred step follows ('each customer gets an email the "
                "next morning'). Handing work to a later consumer is what a "
                "broker is for."
            ),
        },
    ),
    Study2Scenario(
        base=_s(
            id="s2_005_inferred_cache",
            description="Repeated reads over slow-changing data; cache implied, never named",
            request=(
                "It's a price comparison page. The same twenty or so listings "
                "get requested\nthousands of times an hour — everyone lands on "
                "the same handful — and the\nprices behind them only get "
                "refreshed from suppliers three or four times a\nday. Between "
                "refreshes the answer to every request is identical. Around 300 "
                "\npeople on the site at once, roughly 5 GB of listing and price "
                "data. The box\nis 6 GB RAM, 4 vCPU. Nothing regulated.\n"
            ),
            workload_class="CACHING_HEAVY",
            compliance="NONE",
            ram_gb=6,
            vcpu=4,
            concurrent_users=300,
            data_size_gb=5,
            rationale=(
                "Inference: extreme read locality over slow-changing data, "
                "without the word cache appearing."
            ),
        ),
        needs_relational_store=True,
        needs_http_frontend=True,
        needs_repeated_reads=True,
        needs_deferred_work=False,
        coverage="inferred-cache",
        selection_evidence={
            "postgres": (
                "INCLUDE: 'roughly 5 GB of listing and price data' refreshed from "
                "suppliers — a store of record behind the page."
            ),
            "nginx": (
                "INCLUDE: 'around 300 people on the site at once' browsing a web "
                "page."
            ),
            "redis": (
                "INCLUDE, by inference. The word cache never appears, but the "
                "request states the two conditions that justify one and then "
                "states the conclusion: enormous read repetition over a tiny key "
                "set ('the same twenty or so listings... thousands of times an "
                "hour'), data that changes 'three or four times a day', and "
                "'between refreshes the answer to every request is identical'."
            ),
            "rabbitmq": (
                "EXCLUDE: every interaction described is a page request answered "
                "immediately; the supplier refresh is stated as a periodic pull, "
                "not as work handed off."
            ),
        },
    ),
    # -- straightforward ----------------------------------------------------
    Study2Scenario(
        base=_s(
            id="s2_006_analytics_estate",
            description="Ad-hoc analytical estate, no caching or async work",
            request=(
                "Reporting estate for our finance team. About 12 analysts, and "
                "each of them\nwrites their own large aggregate queries across "
                "roughly 90 GB of ledger\nhistory — they're exploratory, so two "
                "people rarely run the same thing twice.\nThey get at it through "
                "a browser-based SQL console. Results come back when\nthey come "
                "back; nobody is waiting on a millisecond budget. Host is 8 GB "
                "RAM,\n4 vCPU. Internal data only, but back it up nightly.\n"
            ),
            workload_class="OLAP",
            compliance="NONE",
            ram_gb=8,
            vcpu=4,
            concurrent_users=12,
            data_size_gb=90,
            backup_required=True,
            rationale="Straightforward OLAP; the interesting judgement is declining a cache.",
        ),
        needs_relational_store=True,
        needs_http_frontend=True,
        needs_repeated_reads=False,
        needs_deferred_work=False,
        coverage="straightforward-olap",
        selection_evidence={
            "postgres": (
                "INCLUDE: 'roughly 90 GB of ledger history' queried with "
                "aggregates."
            ),
            "nginx": (
                "INCLUDE: 'they get at it through a browser-based SQL console' — "
                "an HTTP front end is stated."
            ),
            "redis": (
                "EXCLUDE: 'they're exploratory, so two people rarely run the same "
                "thing twice' — stated absence of repeat locality, which is the "
                "precondition a cache needs. 'Nobody is waiting on a millisecond "
                "budget' removes the latency argument as well."
            ),
            "rabbitmq": (
                "EXCLUDE: queries are run and awaited interactively; no work is "
                "handed to a later stage."
            ),
        },
    ),
    Study2Scenario(
        base=_s(
            id="s2_007_payments_full_stack",
            description="Payments platform needing all four services, PCI DSS",
            request=(
                "This is the platform behind our card payments product. The "
                "checkout itself is\nweb-facing with about 120 people paying at "
                "once at peak, and roughly 30 GB of\ntransaction history has to "
                "be kept. Sessions and the fraud-rules lookup get\nhit on every "
                "single request and the rules themselves barely change from week "
                "to\nweek. Settlement, receipts and the notifications to our "
                "partners all happen\nafter the customer has been told the "
                "payment went through. We are in PCI DSS\nscope: current TLS in "
                "transit, no default credentials anywhere, access logged.\nHost "
                "is 8 GB RAM, 4 vCPU. Backups required.\n"
            ),
            workload_class="OLTP",
            compliance="PCI_DSS",
            ram_gb=8,
            vcpu=4,
            concurrent_users=120,
            data_size_gb=30,
            backup_required=True,
            rationale="Straightforward maximal case: every service is independently justified.",
        ),
        needs_relational_store=True,
        needs_http_frontend=True,
        needs_repeated_reads=True,
        needs_deferred_work=True,
        coverage="straightforward-all-four",
        selection_evidence={
            "postgres": (
                "INCLUDE: 'roughly 30 GB of transaction history has to be kept'."
            ),
            "nginx": (
                "INCLUDE: 'the checkout itself is web-facing with about 120 "
                "people paying at once'."
            ),
            "redis": (
                "INCLUDE: 'sessions and the fraud-rules lookup get hit on every "
                "single request and the rules themselves barely change from week "
                "to week' — per-request reads over near-static data."
            ),
            "rabbitmq": (
                "INCLUDE: 'settlement, receipts and the notifications to our "
                "partners all happen after the customer has been told the payment "
                "went through' — three named workloads deferred past the response."
            ),
        },
    ),
    Study2Scenario(
        base=_s(
            id="s2_008_backend_worker",
            description="Headless ingest worker with no HTTP surface",
            request=(
                "This one has no user interface at all. It's a back-office "
                "component that picks\nup meter readings our field devices "
                "submit through a separate gateway we don't\ncontrol, validates "
                "them, and writes them into the readings store — about 45 GB\nof "
                "history. Devices submit in bursts when they come back online, so "
                "the work\nhas to sit somewhere until a worker gets to it rather "
                "than being dropped.\nRoughly 20 workers running at once. "
                "Nothing is ever read back by a person\nthrough this component. "
                "Host is 6 GB RAM, 4 vCPU. Nightly backups.\n"
            ),
            workload_class="BALANCED",
            compliance="NONE",
            ram_gb=6,
            vcpu=4,
            concurrent_users=20,
            data_size_gb=45,
            backup_required=True,
            rationale=(
                "Straightforward, but excludes the one service every other "
                "scenario needs."
            ),
        ),
        needs_relational_store=True,
        needs_http_frontend=False,
        needs_repeated_reads=False,
        needs_deferred_work=True,
        coverage="straightforward-no-http",
        selection_evidence={
            "postgres": (
                "INCLUDE: 'writes them into the readings store — about 45 GB of "
                "history'."
            ),
            "nginx": (
                "EXCLUDE: 'this one has no user interface at all', and the "
                "device-facing endpoint is 'a separate gateway we don't control'. "
                "There is no HTTP surface for this component to serve."
            ),
            "redis": (
                "EXCLUDE: 'nothing is ever read back by a person through this "
                "component' — the workload is write-only, so there are no repeat "
                "reads to absorb."
            ),
            "rabbitmq": (
                "INCLUDE: 'devices submit in bursts... so the work has to sit "
                "somewhere until a worker gets to it rather than being dropped' — "
                "buffered hand-off to competing consumers, stated as a "
                "requirement."
            ),
        },
    ),
]


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

#: Which ground-truth block each service owns.
_SERVICE_BLOCK = {name: f"expected_{name}" for name in CATALOG_ORDER}


def derive_ground_truth_study2(s2: Study2Scenario) -> dict[str, Any]:
    """Ground truth for one Study 2 scenario.

    Parameter blocks come from the Study 1 derivation unchanged, then
    every block belonging to a service this scenario does not expect is
    dropped. That is the mechanism behind the Study 2 scoring rule: with
    no ``expected_redis`` block, an agent that deploys Redis anyway is
    charged a selection false positive and nothing else, because there
    is no ground truth to score its parameters against.
    """
    expected = s2.expected_services
    truth = derive_ground_truth(s2.base)

    for name, block in _SERVICE_BLOCK.items():
        if name in expected:
            continue
        truth.pop(block, None)

    if "rabbitmq" in expected:
        truth["expected_rabbitmq"] = derive_rabbitmq(s2.base)

    # Ordered so the file reads services-first: the selection decision is
    # the headline of a Study 2 scenario, and the CIS floor closes it.
    truth.pop("expected_cis_minimum_pass_rate", None)
    return {
        "workload_class": truth["workload_class"],
        "compliance": truth["compliance"],
        "expected_services": expected,
        **{k: v for k, v in truth.items() if k.startswith("expected_")},
        "expected_cis_minimum_pass_rate": CIS_MINIMUM[s2.base.compliance],
    }


def build_document(s2: Study2Scenario) -> dict[str, Any]:
    """Assemble the full scenario YAML document."""
    s = s2.base
    return {
        "id": s.id,
        "description": s.description,
        "request": s.request,
        "derivation": {
            "rationale": s.rationale,
            "coverage": s2.coverage,
            "stated_facts": {
                "ram_gb": s.ram_gb,
                "vcpu": s.vcpu,
                "concurrent_users": s.concurrent_users,
                "data_size_gb": s.data_size_gb,
                "backup_required": s.backup_required,
            },
            "selection_facts": {
                "needs_relational_store": s2.needs_relational_store,
                "needs_http_frontend": s2.needs_http_frontend,
                "needs_repeated_reads": s2.needs_repeated_reads,
                "needs_deferred_work": s2.needs_deferred_work,
            },
            "selection_evidence": {
                name: s2.selection_evidence[name] for name in CATALOG_ORDER
            },
            "note": (
                "Ground truth is generated by "
                "benchmark/derive_scenarios_study2.py. Edit the rules there, "
                "not this file. expected_services is derived from "
                "selection_facts, which are in turn evidenced by "
                "selection_evidence against the request text."
            ),
        },
        "ground_truth": derive_ground_truth_study2(s2),
    }


# ---------------------------------------------------------------------------
# Validation and emission
# ---------------------------------------------------------------------------


def validate_documents(documents: list[dict[str, Any]]) -> None:
    """Fail loudly on anything that would corrupt the metrics.

    Checked here rather than in a test because a generator that can emit
    an unscoreable scenario is the fault; the test suite asserts the same
    properties of the written files.

    Raises:
        ValueError: on an unmappable ground-truth key, a service outside
            the catalog, a scenario that selects nothing, a host beyond
            the deployable envelope, missing selection evidence, or a
            parameter block for a service the scenario does not expect.
    """
    from src.experiment.result_logger import GROUND_TRUTH_PARAMETER_MAP, _lookup
    from src.services.catalog import names

    catalog = names()
    if set(catalog) != set(CATALOG_ORDER):
        raise ValueError(
            f"CATALOG_ORDER {CATALOG_ORDER} is stale; catalog is {sorted(catalog)}"
        )

    for doc in documents:
        scenario_id = doc["id"]
        truth = doc["ground_truth"]
        expected = truth["expected_services"]

        if not expected:
            raise ValueError(f"{scenario_id}: expected_services is empty")
        unknown = [name for name in expected if name not in catalog]
        if unknown:
            raise ValueError(f"{scenario_id}: non-catalog service(s) {unknown}")

        ram = doc["derivation"]["stated_facts"]["ram_gb"]
        if ram > MAX_DEPLOYABLE_RAM_GB:
            raise ValueError(
                f"{scenario_id}: {ram} GB exceeds MAX_DEPLOYABLE_RAM_GB"
            )

        for name in CATALOG_ORDER:
            evidence = doc["derivation"]["selection_evidence"].get(name, "")
            if len(evidence) < 40:
                raise ValueError(
                    f"{scenario_id}: selection evidence for {name!r} is missing "
                    "or too thin to defend"
                )
            verb = "INCLUDE" if name in expected else "EXCLUDE"
            if not evidence.startswith(verb):
                raise ValueError(
                    f"{scenario_id}: evidence for {name!r} must start with "
                    f"{verb}, matching expected_services"
                )

        for block, expectations in truth.items():
            if not block.startswith("expected_") or not isinstance(expectations, dict):
                continue
            service = block.removeprefix("expected_")
            if service not in expected:
                raise ValueError(
                    f"{scenario_id}: has {block} but does not expect {service}; "
                    "scoring parameters for an unselected service is exactly "
                    "the conflation Study 2 exists to avoid"
                )
            if block not in GROUND_TRUTH_PARAMETER_MAP:
                raise ValueError(f"{scenario_id}: {block} has no mapping table")
            for key in expectations:
                param, _ = _lookup(block, key)
                if param is None:
                    raise ValueError(
                        f"{scenario_id}: {block}.{key} does not map to a "
                        "final_spec path; it would be silently dropped from "
                        "the correctness denominator"
                    )


def write_all(out_dir: Path = SCENARIO_DIR) -> list[Path]:
    """Regenerate every Study 2 scenario YAML. Returns the paths written."""
    documents = [build_document(s2) for s2 in SCENARIOS]
    validate_documents(documents)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for doc in documents:
        path = out_dir / f"{doc['id']}.yaml"
        path.write_text(
            "# Generated by benchmark/derive_scenarios_study2.py - "
            "do not edit by hand.\n"
            + yaml.dump(doc, sort_keys=False, default_flow_style=False, width=88),
            encoding="utf-8",
        )
        written.append(path)
    return written


if __name__ == "__main__":
    paths = write_all()
    print(f"Wrote {len(paths)} Study 2 scenarios to {SCENARIO_DIR}")
    for path, s2 in zip(paths, SCENARIOS, strict=True):
        print(f"  {path.name:<34} {s2.coverage:<26} {s2.expected_services}")
