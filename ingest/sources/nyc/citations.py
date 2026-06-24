"""NYC citation-URL builders — verifiable links back to authoritative records. NYC-SPECIFIC.

Single responsibility: given the primitive ids a connector already parsed (a Socrata
dataset + primary key, a Borough-Block-Lot), produce :class:`~ingest.extract.schemas.Citation`
objects pointing at the authoritative NYC record. This is the "verifiable" half of
"quantifiable and verifiable": every fact the digest asserts carries a link a resident
(or a reviewer) can click to confirm it against the city's own systems.

Boundary: NYC-SPECIFIC (Rule 4). The *concept* of a citation is city-agnostic and lives
in ``schemas.Citation``; the *URLs* (NYC Open Data, DOB BIS, HPD Online) are NYC knowledge
and live here ONLY. A second city writes its own ``citations.py`` and reuses ``Citation``.

Two tiers, by guarantee (see ``Citation.kind``):
  - ``data_source``  — the EXACT Socrata row, filtered by primary key. Machine-verifiable;
    pins the precise record a claim used. Verified pattern:
    ``/resource/<dataset>.json?<pk>=<value>`` returns that one row as JSON.
  - ``official_lookup`` — a human-facing official nyc.gov page for the same building.
    * DOB permits -> BIS Property Profile (``PropertyProfileOverviewServlet?boro&block&lot``):
      verified to load login-free; shows the building's full DOB job/permit history.
    * HPD violations -> HPD Online. NOTE: HPD Online is a JS single-page app whose
      by-building route requires HPD's internal building id (not BBL/BIN), so it cannot be
      reliably deep-linked from primitives. We therefore point at the HPD Online entry page
      (the correct official tool; the resident searches their address) and rely on the
      row-exact ``data_source`` link for precise verification.
      # TODO Phase 2: resolve BBL -> HPD building id via the HPD Online search API and emit
      # a true deep link; until then the data_source row is the verification guarantee.

No LLM, no network — pure string construction (Rule 1).
"""

from __future__ import annotations

from datetime import datetime

from ingest.extract.schemas import Citation

# --- Authoritative NYC endpoints (NYC-SPECIFIC reference data, Rule 4) ---
_SOCRATA_RESOURCE = "https://data.cityofnewyork.us/resource/{dataset}.json?{pk}={value}"
_BIS_PROPERTY = (
    "https://a810-bisweb.nyc.gov/bisweb/PropertyProfileOverviewServlet"
    "?boro={boro}&block={block}&lot={lot}"
)
# BIS "Permits In-Process / Issued By BIN" — lands directly on a building's permit list,
# keyed by BIN. Better than the general property profile for verifying a specific permit.
_DOB_PERMITS_BY_BIN = (
    "https://a810-bisweb.nyc.gov/bisweb/PermitsInProcessIssuedByBinServlet?requestid=0&allbin={bin}"
)
_HPD_ONLINE = "https://hpdonline.nyc.gov/hpdonline/"


# Hosts we are willing to emit links to (the audit rejects anything else).
KNOWN_HOSTS = (
    "data.cityofnewyork.us",
    "a810-bisweb.nyc.gov",
    "hpdonline.nyc.gov",
    "zap.planning.nyc.gov",
)
# Socrata datasets this module knows; the audit flags an unregistered id.
KNOWN_DATASETS = (
    "wvxf-dwi5",  # HPD housing-maintenance violations
    "ipu4-2q9a",  # DOB permit issuance (legacy BIS system)
    "qnmk-7xra",  # DOB NOW: Build — approved permits (current system)
    "hgx4-8ukb",  # ZAP land-use projects (ulurp_numbers, project_brief, public_status)
    "2iga-a6mk",  # ZAP project-BBL rows (project_id -> bbl, many-to-many)
    "355w-xvp2",  # DOB Local Law 33 energy letter grade (bbl-native, A-F)
    "erm2-nwe9",  # 311 Service Requests (bbl-native; severe-complaint enrichment)
    "tvpp-9vvx",  # NYC permitted event information (rolling 30-day forward window)
)


def socrata_row(
    dataset: str,
    pk_field: str,
    pk_value: str,
    *,
    label: str,
    retrieved_at: datetime | None = None,
) -> Citation:
    """The exact, machine-verifiable Socrata row backing a fact (``verifies='exact_record'``)."""
    return Citation(
        kind="data_source",
        verifies="exact_record",
        label=label,
        url=_SOCRATA_RESOURCE.format(dataset=dataset, pk=pk_field, value=pk_value),
        retrieved_at=retrieved_at,
    )


def dob_permits_by_bin(
    bin_number: str | None, *, retrieved_at: datetime | None = None
) -> Citation | None:
    """BIS "Permits In-Process / Issued By BIN" — the building's permit list (official_lookup).

    Keyed by BIN, a single unambiguous building id, this lands directly on the permits a
    building has. It sidesteps the condo trap that a block/lot property-profile link falls
    into: a permit's tax block/lot can point at a development lot while a resident searching
    the address reaches the condo's billing lot — whose profile reads "no permits" for a
    real permit. ``None`` if no BIN is available (caller falls back to :func:`bis_property`).
    """
    bin_clean = str(bin_number or "").strip()
    if not bin_clean:
        return None
    return Citation(
        kind="official_lookup",
        verifies="exact_building",  # this building's permit list, by BIN
        label="DOB permits for this building (BIS)",
        url=_DOB_PERMITS_BY_BIN.format(bin=bin_clean),
        retrieved_at=retrieved_at,
    )


def bis_property(
    boro_digit: str | None,
    block: str | None,
    lot: str | None,
    *,
    retrieved_at: datetime | None = None,
) -> Citation | None:
    """DOB BIS Property Profile for a building (official_lookup); ``None`` if BBL parts missing."""
    if not (boro_digit and block and lot):
        return None
    # BIS expects unpadded numeric block/lot (e.g. 1617, not 01617); strip when numeric.
    block_n = str(int(block)) if str(block).isdigit() else block
    lot_n = str(int(lot)) if str(lot).isdigit() else lot
    return Citation(
        kind="official_lookup",
        verifies="exact_building",  # deep-links to THIS building's profile
        label="DOB building profile (BIS)",
        url=_BIS_PROPERTY.format(boro=boro_digit, block=block_n, lot=lot_n),
        retrieved_at=retrieved_at,
    )


def hpd_online(*, retrieved_at: datetime | None = None) -> Citation:
    """HPD Online entry page (``kind='official_lookup'``) — resident searches their address.

    Not a deep link by design (see module docstring); the row-exact ``data_source`` link
    is what pins the precise violation.
    """
    return Citation(
        kind="official_lookup",
        verifies="search",  # homepage search tool, not pre-filled — be honest about it
        label="Search this building on HPD Online",
        url=_HPD_ONLINE,
        retrieved_at=retrieved_at,
    )


_ZAP_PORTAL = "https://zap.planning.nyc.gov/projects/{project_id}"


def zap_project(project_id: str, *, retrieved_at: datetime | None = None) -> Citation:
    """DCP ZAP project page — the official canonical view of one land-use application.

    ``kind='official_lookup'`` / ``verifies='exact_building'``: the URL is the
    authoritative city page for this specific project (analogous to a BIS building
    profile but for a land-use application).  The row-exact ``data_source`` Socrata
    link (via :func:`socrata_row`) is what pins the precise record; this link gives
    a resident a human-readable view of the full application history.
    """
    return Citation(
        kind="official_lookup",
        verifies="exact_building",
        label=f"ZAP project {project_id} (NYC Planning portal)",
        url=_ZAP_PORTAL.format(project_id=project_id),
        retrieved_at=retrieved_at,
    )


def audit_citation(c: Citation) -> str | None:
    """Return a problem string if a citation is malformed, else ``None`` (Rule 5 check).

    A structural, offline guard that ``verifiable`` is not just a label: every link
    must be HTTPS to a known official host; an ``exact_record`` link must carry a
    non-empty key query; a Socrata link must name a registered dataset. The eval test
    runs this over every connector-produced citation. (Network liveness — does the row
    still exist? — is a separate, sampled, online check; TODO Phase 2.)
    """
    if not c.url.startswith("https://"):
        return f"non-https url: {c.url}"
    host = c.url.split("/", 3)[2] if c.url.count("/") >= 2 else ""
    if host not in KNOWN_HOSTS:
        return f"unknown host: {host}"
    if "data.cityofnewyork.us/resource/" in c.url:
        dataset = c.url.split("/resource/", 1)[1].split(".json", 1)[0]
        if dataset not in KNOWN_DATASETS:
            return f"unregistered dataset: {dataset}"
    if c.verifies == "exact_record":
        query = c.url.split("?", 1)[1] if "?" in c.url else ""
        key_value = query.split("=", 1)[1] if "=" in query else ""
        if not key_value:
            return f"exact_record link has no key value: {c.url}"
    return None
