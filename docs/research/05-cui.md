# 05 — Controlled Unclassified Information (CUI)

Research date: 2026-10-24. Primary sources only (eCFR, archives.gov/cui, the DoD issuance, NIST CSRC, acq.osd.mil, govinfo.gov, learn.microsoft.com). One DoD fact sheet is used only for DoD framing; where it restates regulation, the regulation is cited instead. Each claim cites a source ID; the source list is at the bottom. **Date-sensitive** facts are marked (DS). **Unverified or inferred** items are marked (?).

---

## TL;DR

1. **CUI is a federal designation, not a level of secrecy.** It is unclassified information that a law, regulation or Government-wide policy requires or permits agencies to protect. It is *CUI Basic* when the authority gives no specific controls and *CUI Specified* when it does. The authority is EO 13556 and 32 CFR Part 2002. The rule binds executive-branch agencies directly and everyone else only through contracts and agreements. [S1]
2. **Access to CUI rests on "lawful government purpose", not on a clearance.** There is no need-to-know test unless a specific authority adds one. Our `Clearance.CUI` role and `authz.clearance_rank` borrow classified-world words for what is really "authorized and trained to handle CUI". [S1][S11]
3. **The banner syntax can be derived exactly from data:** `CUI//SP-A/SP-B/C//LDC1/LDC2`. `//` separates the three elements and `/` separates items within one. Specified categories (prefix `SP-`) come first and Basic categories after, each group in alphabetical order, then the dissemination controls in alphabetical order. Whether a category is Specified depends on **the authority behind it, not the category alone** (Export Controlled can be `SP-EXPT` or `EXPT`). So `cui_categories text[]` cannot render a correct banner by itself. [S2][S5][S6]
4. **Limited Dissemination Controls (LDCs) restrict who may receive the information.** The Registry lists 10: NOFORN, FED ONLY, FEDCON, NOCON, DL ONLY, RELIDO, REL TO, DISPLAY ONLY, Attorney-Client and Attorney-WP. Today our `dissemination` array is displayed but never checked by `authz.readable_collections`. NOFORN, FED ONLY, FEDCON, NOCON, DL ONLY and REL TO all narrow the audience. Enforcing them needs principal attributes we don't model yet: citizenship, affiliation and country. [S3]
5. **Safeguarding baseline:** CUI Basic is at least FIPS 199 *moderate* for confidentiality. On a federal system (including one a contractor operates on an agency's behalf) that means NIST SP 800-53 moderate. On a contractor's own system it means NIST SP 800-171. **DoD still requires 800-171 Rev 2 (110 requirements), not Rev 3 (97).** Rev 2 *requires* FIPS-validated cryptography; Rev 3 only recommends it. (DS) [S1][S14][S15][S22][S23]
6. **CMMC (DS):** Phase 1 began 2025-11-10. Phase 2 was due 2026-11-10, but a DoW CIO memo of 2026-07-13, implemented by Class Deviation 2026-O0025 Rev 3 (signed 2026-09-03), **suspends the Phase 2 transition**. The memo also tells contracting officers to remove or revise CMMC requirements in solicitations and contracts. DFARS 252.204-7012 (800-171 Rev 2, 72-hour incident reporting, a FedRAMP Moderate-equivalent cloud) still applies. [S18][S20][S21][S23]
7. **Hosting:** Commercial Azure and Azure Government both hold FedRAMP High and third-party attestations for 800-171 and DFARS 7012. Only Azure Government adds DoD IL4/IL5, US data residency commitments and screened US-person operators. IL4/IL5 personnel must be US persons, and IL5 needs physical separation from non-government tenants. Azure Storage and Postgres Flexible Server encrypt at rest with AES-256 by default, always on. FIPS 140-2 certificates moved to the CMVP historical list on 2026-09-21. (DS) [S25][S27][S28][S29][S30][S31]

---

## 1. What CUI is

### 1.1 Definition and authority [S1]

- **Authority:** Executive Order 13556 (2010). The implementing rule is 32 CFR Part 2002, in effect since 81 FR 63336 (2016-09-14). NARA's Information Security Oversight Office is the CUI Executive Agent (EA) and runs the CUI Registry.
- **Definition (§ 2002.4(h)):** CUI is information the Government creates or possesses, or that an entity creates or possesses for or on behalf of the Government, that a law, regulation or Government-wide policy requires or permits an agency to handle using safeguarding or dissemination controls.
- **What it excludes:** classified information. It also excludes information a non-executive-branch entity holds in its own systems that did not come from, and was not created for, an agency.
- **Basic vs Specified (§ 2002.4(j), (r)):**
  - *CUI Basic:* the authority says "protect this" but gives no specific controls. The uniform controls in Part 2002 and the Registry apply.
  - *CUI Specified:* the authority spells out its own handling controls. Those controls govern, and Basic controls fill any gaps.
- **Categories (§ 2002.4(k)):** the types of information an authority covers, approved by the EA and listed in the Registry. One category can be Specified while some of its subcategories are not, and vice versa.

### 1.2 Who it applies to [S1][S11]

- **Agencies:** Part 2002 "applies to all executive branch agencies that designate or handle" CUI (§ 2002.1(f)).
- **Everyone else:** non-executive-branch entities are bound only indirectly, "through incorporation into agreements" (§ 2002.1(f)). Agreements must require compliance with the Order, Part 2002 and the Registry, and must require reporting of non-compliance (§ 2002.16(a)(5)–(6)).
- **DoD:** DoDI 5200.48 (effective 2020-03-06) applies the rule to DoD Components and to contracts, grants and other agreements that need access to CUI. For contractor systems it points to DFARS 252.204-7008 and 252.204-7012 [S11 §1.1–1.2].

### 1.3 Access standard: lawful government purpose, not clearance

- **Definition:** "Lawful government purpose" is any activity, mission, function, operation or endeavor that the U.S. Government authorizes or recognizes as within the scope of its legal authorities (§ 2002.4(bb)) [S1].
- **No need-to-know by default:** DoD states that, unlike classified information, an individual "generally does not need to demonstrate a need-to-know to access CUI, unless required by a law, regulation, or government-wide policy" [S11 §3.1.d]. One example of such a law is UCNI, which does impose need-to-know.
- **Who decides:** the person who holds the CUI decides whether a recipient has a lawful government purpose [S11 §3.7.b(1)–(2)].
- **Training:** DoD requires initial and annual refresher CUI training, and records of it [S11 §2.10.d].
- **DoD framing:** the DoD fact sheet [S13] uses the same lawful-purpose test. It says CUI matters because unclassified information, "if publicly associated with defense missions or aggregated with other sources of information, often will reveal exploitable information". One category it mentions applies only to Congress. That detail is outside our scope.

---

## 2. Marking

### 2.1 What Part 2002 requires [S1 § 2002.20]

| Element | Required? | Rule |
|---|---|---|
| CUI control marking | **Mandatory** | `CUI` or `CONTROLLED`, at the designator's choice; agencies may mandate one. Alternative markings are prohibited. |
| Category / subcategory markings | **Mandatory for Specified**, optional for Basic unless agency policy requires them | Every Specified category in the document must appear in the banner. Statements that a law requires (warnings, distribution statements) must **not** go in the banner or portion marks. |
| Limited dissemination control markings | When applied | Only those in the Registry. |
| Banner scope | — | Covers all CUI in the document, the same on every page. |
| Designation indicator | **Mandatory** | Identifies at least the designating agency (letterhead, or a "Controlled by:" line). Must be readily apparent; may appear on the first page or cover only. |
| Decontrol indicator | "Where feasible" | A specific date or a foreseeable, verifiable event, with a POC. |
| Portion markings | **Permitted and encouraged**, not required (in a fully unclassified document) | Inside parentheses, control marking always `CUI`. Once you portion mark, uncontrolled portions are marked `(U)` too. Portion marks with category markings are optional for Basic. |
| Legacy markings (e.g. FOUO, SBU) | **Prohibited** | Void; must not be used. |
| Supplemental administrative markings ("Draft", "Pre-decisional") | Allowed by agency policy | Must not appear inside the CUI marking or duplicate it. |
| Alternate marking for systems | Allowed when individual marking is impractical | E.g. user access agreements or a "digital splash screen" (§ 2002.20(a)(8)). |

**Unmarked CUI is still CUI.** A missing marking does not exempt the holder from handling rules (§ 2002.20(a)(7)).

### 2.2 Banner syntax from the CUI Marking Handbook [S2]

```
CUI//SP-SPECIFIED-A/SP-SPECIFIED-B/BASIC-A/BASIC-B//LDC-A/LDC-B
 │   └──────────── categories ────────────────┘ └── LDCs ──┘
 control marking
```

- `//` (double slash) separates the control marking, the category block and the LDC block.
- `/` (single slash) separates items within a block.
- **Category ordering (mandatory):** "CUI Category and Subcategory Markings MUST be alphabetized within CUI type (Basic or Specified). Alphabetized Specified CUI categories and subcategories MUST precede alphabetized Basic CUI categories". An earlier 2016 printing treated this as optional (?); the current Registry handbook makes it mandatory.
- **`SP-` prefix:** added to a category marking to show that the category is Specified.
- **LDC ordering:** multiple LDC markings "MUST be alphabetized and separated from each other with a single forward slash (/)".
- **Multi-page documents:** the banner is "the sum of all of the CUI" in the document.
- **Typography:** on documents the banner should be bold, capitalized, black and centered. Banner *colours* are not part of the CUI marking scheme. Our `#502b85` purple is a UI convention (?), not a NARA requirement.
- **Portion marks:**
  - `(CUI)`, `(CUI//SP-CTI)`, `(CUI//SP-CTI//NOFORN)`, `(U)`.
  - Commingled with classified information: `(S//CUI//SP-AAAA//DISSEM)`.
  - Classified banner with CUI: `SECRET//CUI//SP-AAAA/BBBB//DISSEM`.
- **Electronic media:** must be marked at least with the control marking and the designating agency.
- **IT systems:** agency heads may authorize alternate marking methods for IT systems, websites and databases ("This system contains CUI" splash or consent banners).
- **Email:** the banner goes at the top of the body, "Contains CUI" may go in the subject line, and markings carry forward on reply and forward [S10].

### 2.3 Category markings (examples) [S4–S9]

The Registry lists **20 index groupings and 126 categories** (DS, page reviewed 2025-03-06) [S4]. Each category page gives a *category marking*. For each authority it then gives whether that authority makes the category Basic or Specified, and the banner that follows.

| Category | Marking | Banner by authority | Note |
|---|---|---|---|
| Controlled Technical Information | CTI | `CUI//SP-CTI` (48 CFR 252.204-7012: Specified) | The core DoD-contractor category [S5] |
| Export Controlled | EXPT | `CUI//SP-EXPT` for Specified authorities, `CUI//EXPT` for Basic ones | **Same category, two banners** [S6] |
| General Privacy | PRVCY | `CUI//SP-PRVCY` or `CUI//PRVCY` | Page reviewed 2026-07-20 (DS) [S7] |
| Geodetic Product Information | GEO | `CUI//SP-GEO` (10 USC 455(b)(1)) | Relevant to geospatial collections [S8] |
| Legal Privilege | PRIVILEGE | `CUI//PRIVILEGE` (Basic) | The only category the Attorney-Client / Attorney-WP LDCs may be used with [S3][S9] |
| Others seen | CRIT, PROPIN / SP-PROPIN, OPSEC, ISVI, SSI (Specified) | — | Fetched to check the pattern (?) — not each cited separately |

**Implication:** "Specified" is a property of the *(category, authority)* pair, not of the category.

### 2.4 Limited Dissemination Controls [S3] (DS, page reviewed 2025-07-14)

| Control | Banner / portion | Meaning (summary) |
|---|---|---|
| No foreign dissemination | `NOFORN` / `NF` | No foreign governments, foreign nationals, foreign or international organizations, or non-US citizens |
| Federal employees only | `FED ONLY` | Executive-branch employees and US armed forces only |
| Federal employees and contractors only | `FEDCON` | FED ONLY plus contractors, for work under their federal contract |
| No dissemination to contractors | `NOCON` | Excludes contractors; still allows state, local and tribal employees |
| Dissemination list controlled | `DL ONLY` | Only those on an accompanying dissemination list. Supersedes other LDCs, but cannot override dissemination set by law. |
| Releasable by information disclosure official | `RELIDO` | Only for agencies eligible to use RELIDO in the IC classified context |
| Authorized for release to certain nationals only | `REL TO [USA, LIST]` | `USA` first, then country trigraphs alphabetically, then tetragraphs (coalitions or organizations) alphabetically. The comma-and-space separator is shown in the examples but not stated as a rule (?). |
| Display only | `DISPLAY ONLY [USA, LIST]` | Listed foreign recipients may view it but may not keep a physical copy |
| Attorney-client | `Attorney-Client` / `AC` | Only with the Legal Privilege category |
| Attorney work product | `Attorney-WP` / `AWP` | Only with the Legal Privilege category |

**Rules on applying LDCs** (§ 2002.16(b)(4)) [S1]:

- Agencies may restrict dissemination **only** through Registry LDCs.
- LDCs may be used only when doing so furthers a lawful Government purpose. Using them to restrict access unnecessarily is "contrary to the goals of the CUI Program". If significant doubt remains after consulting the designating agency's policy, the holder should not apply the LDC.
- **Only the designating agency may apply LDCs.** Others must ask the designating agency for permission.
- Designators may **combine** LDCs.
- **Combination rules are not documented:** the Registry gives no conflict rules beyond the ones above. For example, it says nothing about NOFORN together with REL TO (?).

### 2.5 DoD variations (DoDI 5200.48) [S11]

| Topic | DoD rule | Differs from Part 2002? |
|---|---|---|
| Control marking | At minimum, `CUI` in the banner **and footer** of each page (§3.4.a, f) | DoD picks `CUI` over `CONTROLLED` and adds the footer |
| Basic vs Specified | "During DoD's initial phased implementation … there is no required distinction" between Basic and Specified; all DoD CUI is protected at Basic unless a law says otherwise (§3.4.g). Whether a later DoD manual changes this is not confirmed (?). | Yes, during the phase-in |
| Designation indicator | A five-line block on the first page (Figure 2):<br>`Controlled by: [Component]` (omit if on letterhead)<br>`Controlled by: [Office]`<br>`CUI Category: [list]`<br>`Distribution/Dissemination Control: [...]`<br>`POC: [phone or email]` | Adds category, dissemination and POC lines |
| Portion marks | Optional, but when used `(U)` is required on uncontrolled portions. Banners no longer use `U//FOUO`. | Same idea, stricter phrasing |
| Classified documents | CUI appears only in portion marks `(CUI)`, not in the banner; a warning box notes the CUI | **Differs** from the NARA commingling banner (`SECRET//CUI//…`) [S2] |
| CTI and distribution statements | CTI keeps DoDI 5230.24 Distribution Statements B–F. Export-controlled data also needs an export-control warning. Table 2 maps LDCs to statements: FED ONLY→B, FEDCON→C, DL ONLY→F; DISTRO X is cancelled. | DoD-specific, alongside LDCs |
| Systems | DoD systems holding CUI are moderate confidentiality under DoDIs 8500.01/8510.01 and must show a **splash screen warning and notice of consent** (§3.10.c–d) | Turns the NARA "may" into a "must" |
| Registry | The DoD CUI Registry "mirrors the National CUI Registry" and aligns each index and category to DoD issuances (§3.3.d). The official copy is on Intelink (§3.3.d(1)); the public site is dodcui.mil [S12]. | Adds mappings, not new categories (?) |
| Aggregation | Original classification authorities decide whether *aggregated* CUI should be classified (§3.3.b). Contracts must require contractors to monitor CUI for aggregation and report potential classification (§5.3.c). CTI "compiled or aggregated may become classified" (§3.7.c). | DoD-specific duty |

I could not fetch dodcui.mil (HTTP 403 to automated clients). So I could not compare the DoD Registry with the NARA Registry entry by entry (?).

---

## 3. Safeguarding

### 3.1 Baseline and which control set applies [S1 § 2002.14]

- **Confidentiality baseline:** CUI Basic is "no less than the moderate confidentiality impact level" under FIPS 199 (§ 2002.14(g)).
- **Raising the baseline:** agencies may go above moderate internally or by agreement. Outside the agency they may not demand more for CUI Basic.
- **Federal information system (§ 2002.14(h)(1)):** a system used or operated by an agency, or by a contractor *on behalf of* an agency. Such a system is treated as the agency's own system, so FIPS 200 and NIST SP 800-53 apply, and in cloud the FedRAMP / ATO route.
- **Non-Federal information system (§ 2002.14(h)(2)):** any other system. Agencies "must use NIST SP 800-171" to set requirements for it.
- **For this platform:** which regime applies depends on who operates the system and for whom. If a contractor runs it as a service to an agency, it is a Federal system (800-53 moderate). If a contractor runs it for its own contract work on DoD CUI, it is non-Federal (800-171 through DFARS 7012).
- **Physical and procedural measures (§ 2002.14(c)):** controlled environments, at least one physical barrier, and preventing unauthorized observation.
- **Destruction (§ 2002.14(f)):** CUI must be made "unreadable, indecipherable, and irrecoverable", using NIST SP 800-88 / 800-53, or a method approved for classified information. Only when the agency no longer needs it **and** NARA records schedules allow.
- **Decontrol (§ 2002.18):**
  - Done by the designating agency, automatically or by decision.
  - Decontrol "does not constitute authorization for public release".
  - Unauthorized disclosure is not decontrol.
  - Agencies must not decontrol to conceal a disclosure.
  - Decontrolled information reused in a new document must have all CUI markings removed.

### 3.2 NIST SP 800-171: which revision (DS)

| | Rev 2 (Feb 2020, upd. 2021-01-28) | Rev 3 (2024-05-14) |
|---|---|---|
| Requirements | 110 (§3.1–3.14) [S15][S18] | 97 active (130 identifiers, 33 withdrawn) across **17 families**, now with organization-defined parameters [S14] |
| Crypto (3.13.11 / 03.13.11) | "**Employ FIPS-validated cryptography** when used to protect the confidentiality of CUI" [S15] | "Implement … [organization-defined types of cryptography]"; FIPS-validated cryptography is "**recommended**" [S14] |
| Assessment | 800-171A (2018) | 800-171A Rev 3 (May 2024) [S16] |
| Used by DoD? | **Yes.** Class Deviation 2024-O0013 Rev 1 (2024-05-22) fixes DFARS 7012 to Rev 2 "until rescinded" [S22]. 32 CFR 170 incorporates Rev 2 by reference [S18]. The 2026-07-13 CIO memo "requires baseline compliance with NIST SP 800-171 Rev 2" [S23]. | Not adopted by DoD as of 2026-10-24 |

**SP 800-172 Rev 3** (final May 2026) gives *enhanced* requirements for CUI tied to critical programs or high-value assets. CMMC Level 3 still references the Feb 2021 edition (24 requirements) [S17][S18]. It does not apply to this platform unless a contract names it.

### 3.3 The families that matter most here (Rev 3 numbering, Rev 2 in brackets) [S14][S15]

| Requirement | What it asks | Where it lands in this platform |
|---|---|---|
| 03.01.01–.07 Account mgmt, access enforcement, least privilege [3.1.1–3.1.7] | Authorized users only; least privilege; privileged functions separated | Entra app roles, `authz.readable_collections`, Admin never bypasses clearance |
| **03.01.03 Information flow enforcement** [3.1.3] | "Enforce approved authorizations for controlling the flow of CUI within the system and between connected systems" | Derivatives, search index, tiles, exports and Azure Monitor mirroring are all *flows* |
| 03.01.09 System use notification [3.1.9] | Login banner | DoD makes the CUI splash and consent screen mandatory [S11 §3.10.d] |
| 03.01.22 Publicly accessible content [3.1.22] | Keep CUI off public endpoints | STAC API and tile endpoints must stay behind auth |
| **03.03.01–.08 Audit** [3.3.1–3.3.9] | Event logging. Record content: type, when, where, source, outcome and identity (03.03.02). Time stamps (03.03.07). Protect audit information and limit audit management to "a subset of privileged users" (03.03.08). | `audit.events`, ACC-5, ACC-6 |
| 03.05.03 MFA [3.5.3] | MFA for all network access (Rev 2 split privileged and non-privileged) | Entra Conditional Access; not app code |
| 03.08.03 Media sanitization; **03.08.04 Media marking** [3.8.3, 3.8.4] | Sanitize; "mark system media that contain CUI to indicate distribution limitations, handling caveats, and applicable CUI markings" | Blob deletion vs soft delete and immutability (§5.6); blob metadata/tags carrying markings |
| 03.13.08 Transmission and storage confidentiality [3.13.8, 3.13.16] | Cryptography in transit (TLS/IPsec) and at rest | Azure SSE (§4.3); TLS to Postgres, Blob and Entra |
| 03.13.11 Cryptographic protection [3.13.11] | See §3.2; Rev 2 needs **FIPS-validated** cryptography | Platform crypto is Microsoft's; the app's own TLS stack (Python/OpenSSL in containers) is ours (?) |
| 03.14.02 Malicious code protection [3.14.2] | Scan for malware | Defender for Storage / ClamAV before promotion (D3) |

### 3.4 Incident reporting (DS) [S19][S24][S11]

- **DFARS 252.204-7012 (MAY 2024 clause):**
  - "Rapidly report" means **within 72 hours of discovery**, at `dibnet.dod.mil`, using a DoD-approved medium-assurance certificate.
  - The contractor must preserve images of affected systems and "all relevant monitoring/packet capture data for **at least 90 days**" after the report.
  - On request, the contractor must give DoD access for forensic analysis.
- **DFARS 252.239-7010 (cloud services for DoD):**
  - Report all cyber incidents related to the cloud service through DIBNet.
  - Keep Government data in the US unless authorized otherwise.
  - The clause defines *spillage* as CUI moving onto a system not authorized for it.
- **DoD unauthorized disclosure:** misuse or unauthorized disclosure goes to the Component's Unauthorized Disclosure program. The originating Component is informed, and the Military Department counterintelligence organization is notified of all incidents [S11 §3.5.a(4), §3.9.e].

### 3.5 CMMC status (DS) [S18][S20][S21][S23]

- **The rules:**
  - The 32 CFR Part 170 program rule (89 FR 83214, 2024-10-15) defines Levels 1–3. Level 2 is the 110 Rev 2 requirements; Level 3 adds 24 requirements from 800-172.
  - The 48 CFR acquisition rule (FR document 2025-17359, published 2025-09-10) took effect **2025-11-10** and added clause 252.204-7021 (NOV 2025).
- **Phase-in (§ 170.3(e)):**
  - Phase 1 began on the 48 CFR rule's effective date (2025-11-10): Level 1/2 self-assessments.
  - Phase 2 was due one year later (2026-11-10): Level 2 C3PAO certification.
  - Phase 3 and Phase 4 (full) were to follow at one-year intervals.
- **Suspension:** Class Deviation 2026-O0025 Rev 3 (signed 2026-09-03) implements the DoW CIO memo *Suspension of the Advancement to CMMC Phase 2 Requirements* (2026-07-13). The memo:
  - permits Level 1/2 (Self) requirements;
  - requires baseline 800-171 Rev 2 under DFARS 7012;
  - "suspends the November 2026 CMMC Phase 2 transition";
  - has contracting officers "remove or revise" CMMC requirements in new and existing solicitations and contracts.
- **Not read:** I read the deviation memo but not the CIO memo itself (?).
- **Cloud under CMMC Level 2:** a cloud service provider must be FedRAMP Moderate (or higher) authorized, or meet "security requirements equivalent" to that baseline (§ 170.16(c)(2), § 170.17(c)(5)) [S18].

---

## 4. Hosting

### 4.1 What the authorizations mean for CUI

| Authorization | What it covers | Source |
|---|---|---|
| FedRAMP Moderate | The floor that DFARS 7012 sets for any external CSP holding covered defense information ("equivalent to … FedRAMP Moderate baseline"). The CMMC Level 2 CSP rule uses the same floor. | [S19][S18] |
| FedRAMP High | Held by **both** commercial Azure and Azure Government (JAB P-ATO). Microsoft says FedRAMP High covers 800-171, and a 3PAO attests both clouds meet 800-171 and DFARS 7012. | [S25][S26] |
| DoD IL4 | CUI, non-critical mission information, non-national-security systems; up to moderate confidentiality and integrity. A FedRAMP High P-ATO is accepted toward an IL4 PA, plus the SRG's other requirements. **Azure Government only.** | [S27] |
| DoD IL5 | CUI needing more protection than IL4, plus national security systems. **Physical separation from non-government tenants.** Azure Government regions need the IL5 isolation guidance; the US DoD Central and East regions carry a separate IL5 PA. | [S28] |
| IL4/IL5 personnel | CSP personnel with access must be US citizens, US nationals or US persons (SRG §5.6.2) | [S27][S28] |
| DFARS 252.239-7010 | DoD cloud contracts must meet the DoD Cloud Computing SRG at the required impact level, and keep data in the US | [S24] |

**Microsoft's own caveat** [S25]: "Microsoft doesn't inspect, approve, or monitor your Azure applications. You're responsible for ensuring that your CUI workloads comply". The CSP's authorization covers the platform. It does not cover our application.

### 4.2 Azure Government vs commercial [S25][S26][S32]

- **The same in both clouds:** the security controls and encryption features, and FedRAMP High.
- **Azure Government adds:**
  - contractual US storage of customer data;
  - operators who are screened US persons. That means verified US citizenship, a Tier 3 investigation for IL4/IL5 operators, just-in-time access and Customer Lockbox;
  - a physically isolated network;
  - IL4/IL5 PAs.
- **ITAR and U-NNPI:** this is the case that forces Gov. Microsoft says Azure Government "can accommodate" U-NNPI (which is marked NOFORN), and Naval Reactors must authorize hosting first [S25].
- **Service scope:** see doc 02 §1.2. **Container Apps is FedRAMP High + IL2 only in Gov, not IL4/IL5**, which matters if a customer requires IL4 or IL5.
- **Identity:** Entra in Azure Government is a separate authority (`login.microsoftonline.us`, doc 02 §1.1). How it relates to GCC High tenants is not covered here (?).

### 4.3 Encryption at rest and FIPS 140 (DS)

- **Blob Storage** [S30]:
  - Service-side encryption uses 256-bit AES (AES-GCM) and "is FIPS 140-2 compliant". It is on for every account and "You can't disable" it. Object metadata is encrypted too.
  - Options: customer-managed keys in Key Vault or Managed HSM; encryption scopes per container or blob; infrastructure (double) encryption.
- **Postgres Flexible Server** [S31]:
  - "Always encrypts all data at rest", including WAL, server logs and backups, through Azure disk server-side encryption.
  - Customer-managed key (CMK) mode can be chosen **only at server creation** and can't be reverted.
  - Managed HSM is FIPS 140-3 validated.
- **What "validated" means** [S29]:
  - A cloud service can't itself be FIPS 140 validated. Azure services rely on validated Windows cryptographic modules (e.g. BCryptPrimitives) and call themselves "FIPS 140 compliant".
  - Key Vault vaults use FIPS 140 Level 2 HSMs; Managed HSM uses Level 3.
  - Microsoft "doesn't enforce FIPS Mode by default".
- **Certificate transition:** FIPS 140-2 validations moved to the CMVP historical list on **2026-09-21**. Existing systems may keep using them; new procurements should prefer 140-3 [S29].
- **Gaps in this research:** Gov-cloud TLS (1.2, FIPS-validated algorithms between Gov datacenters) [S32]. The FIPS status of our own containers' OpenSSL is unverified (?).

---

## 5. Implications for this platform

Everything in this section is a **recommendation**. Nothing in the code, migrations or spec has been changed.

### 5.1 Is `cui_categories text[]` + `dissemination text[]` enough?

**Not yet.** It records *that* there are categories and controls, but it falls short in five ways:

1. **Specified is per authority.** `EXPT` vs `SP-EXPT` depends on which authority applies (§2.3), so the banner can't be rendered from the category codes alone.
2. **No vocabulary check.** Any string is accepted. Part 2002 forbids markings outside the Registry, and legacy `FOUO` is void (§2.1).
3. **No designation indicator** (designating agency, office, POC) and **no decontrol indicator**, though both are mandatory or "where feasible".
4. **LDCs aren't plain strings.** `REL TO` and `DISPLAY ONLY` carry country lists, and `DL ONLY` carries a list of people.
5. **DoD distribution statements (B–F) have no home.** They are separate from LDCs and must not go in the banner (§2.1, §2.5).

**Recommendation:** model markings as data, the same way `authz.markings` already does.

- `authz.cui_categories`: the code, name, index grouping, and whether it *can* be Basic, Specified or both. Seed it from the NARA Registry, which is a public-domain list; don't retype it (AGENTS.md "data, not code").
- `authz.ldcs`: the code, banner text, portion text, and whether it takes a list. Add a CHECK so Attorney-Client and Attorney-WP only go with `PRIVILEGE`.
- On the collection:
  - `cui_categories` becomes a set of `(category_code, specified boolean)`, with a foreign key to the registry table;
  - `ldcs` holds codes plus an optional country or tetragraph list;
  - `designated_by` / `controlled_by_office` / `poc`;
  - `decontrol_on` (date or event text);
  - an optional `distribution_statement` (`A`–`F`) for CTI.
- Keep the existing CHECK that CUI details appear only on CUI collections.

### 5.2 Deriving the banner

The banner can be derived exactly and deterministically. That suits a pure function plus a property test, with no hand-typed strings.

```
banner(m) =
  "UNCLASSIFIED"                                   if m.level = UNCLASSIFIED   (UI convention)
  "CUI" + ("//" + join("/", sort(SP-cats) ++ sort(basic-cats)) if any cats)
        + ("//" + join("/", sort(ldc banners))                if any LDCs)
```

**Recommendations:**

- Put the function in one place, SQL or Python, and generate the web type from the API. Don't re-implement it in TypeScript (AGENTS.md duplication rule).
- Sort REL TO lists `USA` first, then trigraphs alphabetically, then tetragraphs.
- Add property tests:
  - the banner of an aggregate equals the banner of the union of its parts;
  - the order is stable;
  - parsing a rendered banner returns the original marking.
- Decide whether Basic category markings appear in the banner. Part 2002 leaves it optional and DoD puts categories in the indicator block instead. Make it a per-deployment setting with a documented default (?).
- The page banner (ACC-3) should be the **union** of categories and LDCs across everything on screen, not just the highest `rank`. The handbook's "sum of all CUI" rule says so [S2].

### 5.3 Marking order: "raise never lower" needs a lattice, not a rank

`authz.markings.rank` is a total order (0 for UNCLASSIFIED, 10 for CUI). With categories and LDCs, markings are only *partially* ordered. Item ≥ collection should mean all three of:

- **Level:** item level ≥ collection level (today's rank check).
- **Categories:** item categories ⊇ collection categories, and a category that is Specified on the collection stays Specified on the item.
- **Audience:** the item's permitted audience ⊆ the collection's. Compare the *audience* each LDC set allows, not the strings:
  - NOFORN, FED ONLY, FEDCON, NOCON and DL ONLY only ever shrink the audience;
  - FED ONLY is narrower than FEDCON;
  - a REL TO list shrinks as countries are removed;
  - NOFORN is narrower than any REL TO.

  Whether NOFORN and REL TO may appear together is not settled by the Registry (?). Reject that combination until a source says otherwise.

**Recommendation:** implement the comparison as **one** SQL function, `authz.marking_covers(outer, inner)`. Use it in the ACC-3 CHECK/trigger, for derivatives (ACC-3, M3) and for upload promotion, so the ordering rule exists once. Add the independent version to `tests/model.py`. Add a mutant that compares only `rank` and so lets an item drop `SP-CTI`.

**Lowering is a separate action.** Lowering a marking is *decontrol* or *re-designation*. Only the designating agency's authorized personnel may do it (§ 2002.18(d)), and it never authorizes public release (§ 2002.18(e)). Model it as its own audited action with a reason, not as an edit that ACC-4 happens to allow.

### 5.4 Access: what to enforce, and the "clearance" naming

- **The naming suggests a clearance.** `Clearance.CUI` and `authz.clearance_rank` read like a security clearance. Under Part 2002 and DoDI 5200.48 the real test is lawful government purpose plus required training (§1.3). There is no clearance and, by default, no need-to-know.
- **What the role actually encodes:** "this person has been authorized for CUI handling and has completed CUI training". The grant on the collection then expresses the lawful-purpose decision.
- **Recommendation (naming only):** either rename (e.g. `CuiAuthorized`, `authz.cui_authorization_rank`, `INGEST_OIDC_AUTHORIZATION_CLAIM`) or add a short note in `docs/dev/oidc.md` and `00-requirements-brief.md` D3 saying so. A rename touches the migrations (add a new migration; never edit merged ones), the Keycloak realm, the settings and the docs. Weigh that churn against a doc note.
- **Recommendation (enforcement):** treat audience-restricting LDCs as **hard access attributes in `authz.readable_collections`**, not as display-only text. Each needs a principal attribute from OIDC claims, handled as data in the same way as clearance:

  | LDC | Principal attribute needed | Notes |
  |---|---|---|
  | NOFORN | `us_person` (citizenship) | Entra has no standard claim; needs a directory extension attribute (?) |
  | FED ONLY / FEDCON / NOCON | `affiliation` ∈ {federal, armed forces, contractor, SLTT} | FEDCON also needs "under a federal contract", which is really the grant |
  | DL ONLY | — | Maps naturally to `principal_type = 'user'` grants only: refuse role or group grants on DL ONLY collections |
  | REL TO [list] | `country` (trigraph) | `USA` always passes |
  | DISPLAY ONLY [list] | `country` | Listed foreign viewers may view but get **no download or export signed URL**. Fits ACC-8: issue no read SAS for blobs. |
  | Attorney-Client / Attorney-WP | — | No new attribute; vocabulary CHECK only |

  If the platform does **not** enforce an LDC, the UI and the docs should say so plainly and the API should refuse to accept that LDC. The other option, showing a NOFORN banner while letting foreign users read the data, is worse than having no marking.
- **Invariant:** extend ACC-1 to include the LDC audience predicate, and give `tests/model.py` its own independent version. A new invariant (e.g. "ACC-9 Dissemination controls narrow audience") belongs in `03-invariants.md` first.

### 5.5 Aggregation and derived data

- **What DoD says:** unclassified data aggregated with other sources can reveal exploitable information [S13]. DoD makes aggregation an explicit concern: original classification authorities review it, contractors monitor and report it, and CTI can become classified by compilation [S11 §3.3.b, §3.7.c, §5.3.c].
- **Where a catalog aggregates:** search results, facet counts, map footprints, vector-tile layers and semantic-search neighbours all combine many records.
- **Filter before aggregating.** Every search, facet count, tile and STAC response must filter through `authz.readable_collections` *before* aggregation (ACC-1). Counts must not include unreadable items, or they leak existence (ACC-2). Add sim checks that facet totals equal the count of readable matches.
- **Derived data inherits the source marking.** Thumbnails, 3D Tiles, COG overviews, extracted EXIF/GDAL/IFC metadata, full-text `fts` vectors, embeddings and footprints all take at least the source file's marking (ACC-3 for derivatives). Embeddings and extracted text are easy to forget because they don't look like files.
- **Blob metadata.** Write the marking into blob index tags or metadata on originals and derivatives. That marks the "media" (03.08.04) in a way storage-side tools and lifecycle rules can see.
- **What the platform does not do.** It cannot decide whether an aggregate becomes *classified*. That is an original classification authority's call. Say so in the docs, and offer an export of "what this principal could see" so an authorized reviewer can make that call (?).

### 5.6 Destruction, retention and decontrol

- **The conflict:** retention (D4) keeps originals immutable, versioned and soft-deleted, and legal hold uses immutability policies. CUI destruction must make data irrecoverable (NIST 800-88), but only once NARA records schedules allow it (§ 2002.14(f)).
- **Recommendation:** record the records schedule or disposition authority on the collection.
  - Make "destroy" an audited action that purges all versions and soft-deleted copies after legal hold is released, including derivatives.
  - For cloud media, whether deleting a CMK or encryption-scope key counts as 800-88 "purge" (crypto-erase) for our purposes is unverified (?).
- **Decontrol:** store `decontrol_on`. An expired date can automatically show the item as decontrolled (§ 2002.20(e)(2)), but that does **not** make it public (§ 2002.18(e)).

### 5.7 What the audit log needs

`audit.events` already covers the 03.03.02 basics: `action` (type), `at` (when), `ip`/`request_id` (where and source), `outcome` and `actor_sub` (identity). It is append-only (ACC-6). Recommendations:

- **Record the full marking, not just the level.** Store the categories, LDCs and banner disclosed by the request, in `detail` or new columns, so downloads and exports record the exact marking (D3). Today `marking text` holds only `CUI`.
- **Treat the log as CUI.** Titles, ids and search terms in `detail` can themselves be CUI. Restrict reading `audit.events` to a small auditor role. 03.03.08 limits audit *management* to "a subset of privileged users".
- **Time source:** Postgres `now()` comes from the server's clock. Document that the server syncs to an authoritative time source (03.03.07).
- **Retention for incident response:** keep audit and request logs long enough to support the 72-hour report and the 90-day preservation duty (§3.4). Set the minimum retention to 90 days *after* any incident report, and longer by policy. Mirror to Azure Monitor / Log Analytics with its own retention.
- **New action types:** `decontrol`, `remark` (raising), `destroy`, `ldc_refused` (a refusal for dissemination reasons rather than grant reasons), and an export of who saw a given collection, for spill response.
- **Protect the log against superusers too.** The append-only trigger stops the app role. A superuser can still disable triggers. Document that and rely on the Azure Monitor copy for tamper evidence (?).

### 5.8 UI marking

- **Banner and footer.** Show the derived banner (§5.2) at the top **and bottom** of every page that shows CUI (DoD §3.4.a).
- **Splash screen.** Add a CUI splash and consent screen at sign-in when any CUI collection exists (DoD §3.10.d; 03.01.09).
- **Item page indicator block.** Show a designation-indicator block on the item page: "Controlled by", category, dissemination or distribution, POC.
- **Downloads.** Mark downloaded files where the format allows: a sidecar file, or embedded metadata (GeoTIFF tags, glTF `extras`). Treat that as a later milestone.

### 5.9 What the docs should say we do **not** claim

Add a short block to `00-requirements-brief.md` D3 and the README:

- No ATO, no FedRAMP authorization, no DoD PA and no CMMC status. No 800-171 System Security Plan or assessment (SPRS score) exists.
- The platform is not a designating agency and does not decide whether information *is* CUI. It records the marking an authorized user assigns.
- Sample and seed data are **not** CUI, whatever they are marked. The local stack (Keycloak, Azurite, Compose) and the commercial-Azure demo must never hold real CUI.
- Cryptography relies on Azure's FIPS 140-compliant service encryption. Application-layer TLS has not been validated as FIPS-validated.
- LDC enforcement covers only the controls listed as enforced (§5.4). The others are refused, not shown as if enforced.
- The platform does not determine aggregation or classification.
- Hosting real DoD CUI at IL4/IL5 would also need AKS instead of Container Apps (doc 02 §1.2) and the IL5 isolation configuration.

---

## Open questions / to verify

1. **The DoD CUI Registry.** dodcui.mil returns 403 to automated fetches. Compare its category list and any DoD-only guidance by hand.
2. **DoDI 5200.48 updates.** Has it changed since 2020-03-06, or does a DoD manual end the "no Basic/Specified distinction" phase-in? The official esd.whs.mil PDF also returned 403; the text here was read from a Marine Corps–hosted copy [S11].
3. **The CIO memo.** Fetch the full DoW CIO memo of 2026-07-13 on suspending CMMC Phase 2, and watch for 2024-O0013 being rescinded or 800-171 Rev 3 being adopted.
4. **LDC conflicts.** Does NARA or DoD give conflict rules for combining LDCs (NOFORN with REL TO; FED ONLY with FEDCON)?
5. **Entra attributes.** Which attributes can carry citizenship, affiliation and country in Entra (Gov), and is GCC High required for the identity tenant of a DoD CUI system?
6. **Crypto-erase.** Is CMK or encryption-scope key deletion acceptable as NIST 800-88 purge for Blob and Postgres backups?
7. **FIPS in our containers.** What is the FIPS status of the OpenSSL / Python `ssl` in our containers? Is a FIPS-enabled base image needed for Rev 2's 3.13.11?
8. **The fact sheet's URL.** Find the public URL of the DoD fact sheet [S13].

---

## Sources

| ID | URL | Page date / updated |
|---|---|---|
| S1 | https://www.ecfr.gov/current/title-32/subtitle-B/chapter-XX/part-2002 (read via eCFR API `api/versioner/v1/full/2026-10-22/title-32.xml?part=2002`) | eCFR up to date as of 2026-10-22; source 81 FR 63336 (2016-09-14) |
| S2 | https://www.archives.gov/files/cui/documents/20161206-cui-marking-handbook-v1-1-20190524.pdf | Handbook v1.1, 2016-12-06; posted 2019-05-24 |
| S3 | https://www.archives.gov/cui/registry/limited-dissemination | Last reviewed 2025-07-14 |
| S4 | https://www.archives.gov/cui/registry/category-list | Last reviewed 2025-03-06 |
| S5 | https://www.archives.gov/cui/registry/category-detail/controlled-technical-info | Last reviewed 2025-05-08 |
| S6 | https://www.archives.gov/cui/registry/category-detail/export-control | Last reviewed 2025-05-08 |
| S7 | https://www.archives.gov/cui/registry/category-detail/privacy | Last reviewed 2026-07-20 |
| S8 | https://www.archives.gov/cui/registry/category-detail/geodetic-product-info | Last reviewed 2025-07-25 |
| S9 | https://www.archives.gov/cui/registry/category-detail/legal-privilege | Last reviewed 2025-07-29 |
| S10 | https://www.archives.gov/files/cui/documents/cui-email-marking-tip-20180605.pdf | 2018-06-05 |
| S11 | DoDI 5200.48, *Controlled Unclassified Information (CUI)*. Official: https://www.esd.whs.mil/Portals/54/Documents/DD/issuances/dodi/520048p.PDF (403 to automated fetch). Read from: https://www.hqmc.marines.mil/Portals/61/Docs/HQSVCBN%20Henderson%20Hall/Security/Resources/DoDM%205200.48%20-%20CUI.PDF | Effective 2020-03-06 |
| S12 | https://www.dodcui.mil/ (DoD CUI Registry, public site; 403 to automated fetch, not read) (?) | — |
| S13 | OUSD(I&S), *Controlled Unclassified Information (CUI)*, 2-page fact sheet for Members of Congress and staff. Public URL not found (?). | Created 2023-04-03 |
| S14 | https://csrc.nist.gov/pubs/sp/800/171/r3/final (PDF: https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-171r3.pdf) | 2024-05-14 |
| S15 | https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-171r2.pdf | Feb 2020; updated 2021-01-28 |
| S16 | https://csrc.nist.gov/pubs/sp/800/171/a/r3/final | May 2024 |
| S17 | https://csrc.nist.gov/pubs/sp/800/172/r3/final | Final 2026-05-13 |
| S18 | https://www.ecfr.gov/current/title-32/subtitle-A/chapter-I/subchapter-G/part-170 (read via eCFR API) | eCFR as of 2026-10-22; source 89 FR 83214 (2024-10-15) |
| S19 | https://www.ecfr.gov/current/title-48/chapter-2/subchapter-H/part-252/subpart-252.2/section-252.204-7012 | Clause dated MAY 2024; eCFR as of 2026-10-22 |
| S20 | https://www.ecfr.gov/current/title-48/chapter-2/subchapter-H/part-252/subpart-252.2/section-252.204-7021 and .../section-204.7502 | Clause NOV 2025 (90 FR 43575, 2025-09-10) |
| S21 | https://www.govinfo.gov/content/pkg/FR-2025-09-10/html/2025-17359.htm (DFARS CMMC final rule) | Published 2025-09-10; effective 2025-11-10 |
| S22 | https://www.acq.osd.mil/dpap/policy/policyvault/USA001074-24-DPC.pdf (Class Deviation 2024-O0013 Rev 1) | 2024-05-22 |
| S23 | https://www.acq.osd.mil/dpap/dars/classdev/DFARS_RFO/Part-240/2026-O0025_Rev3_TAB_A_Deviation_Memo.pdf (Class Deviation 2026-O0025 Rev 3) | Signed 2026-09-03 |
| S24 | https://www.ecfr.gov/current/title-48/chapter-2/subchapter-H/part-252/subpart-252.2/section-252.239-7010 | Clause JAN 2023; eCFR as of 2026-10-22 |
| S25 | https://learn.microsoft.com/en-us/azure/compliance/offerings/offering-nist-800-171 | ms.date 2023-06-12; updated 2024-10-10 |
| S26 | https://learn.microsoft.com/en-us/azure/compliance/offerings/offering-dfars | ms.date 2023-06-12; updated 2024-10-10 |
| S27 | https://learn.microsoft.com/en-us/azure/compliance/offerings/offering-dod-il4 | ms.date 2023-04-04; updated 2024-10-10 |
| S28 | https://learn.microsoft.com/en-us/azure/compliance/offerings/offering-dod-il5 | ms.date 2023-04-04; updated 2024-10-10 |
| S29 | https://learn.microsoft.com/en-us/azure/compliance/offerings/offering-fips-140-2 | ms.date 2023-06-12; updated 2024-10-10 |
| S30 | https://learn.microsoft.com/en-us/azure/storage/common/storage-service-encryption | ms.date 2026-08-11; updated 2026-08-15 |
| S31 | https://learn.microsoft.com/en-us/azure/postgresql/security/security-data-encryption | ms.date 2026-09-18; updated 2026-09-20 |
| S32 | https://learn.microsoft.com/en-us/azure/azure-government/documentation-government-plan-security | ms.date 2022-03-21; updated 2026-07-09 |
