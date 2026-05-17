# GLEIF Parent-Matching Design Note

## 1. Problem statement

Given a list of global company names from an internal database, identify:

1. The matching **GLEIF legal entity** and **LEI**
2. Whether that LEI represents an **active legal entity**
3. The entity’s reported:
   - **Direct accounting consolidating parent**
   - **Ultimate accounting consolidating parent**
4. The parent’s:
   - **Parent LEI**
   - **Parent legal name**
5. The corresponding **internal parent/company rollup**, by matching the parent LEI or parent name back into the internal company database.

This requires relating:

```text
Internal Company Database
        ↓ name/entity matching
GLEIF Level 1 LEI-CDF
        ↓ child LEI → parent LEI relationship
GLEIF Level 2 RR-CDF
        ↓ parent LEI lookup
GLEIF Level 1 LEI-CDF
        ↓ parent match / rollup
Internal Company Database
```

GLEIF Level 1 answers **“who is who”**.  
GLEIF Level 2 Relationship Records answer **“who owns whom”**.

---

# 2. Core relationship logic

## 2.1 Level 1: identify the legal entity and LEI

Use the **GLEIF Golden Copy Level 1 LEI-CDF** file to match internal company names to LEI records.

Primary Level 1 fields used for entity matching:

| Purpose | GLEIF field |
|---|---|
| Stable entity key | `LEI` |
| Primary legal name | `Entity.LegalName` |
| Alternate names | `Entity.OtherEntityNames.OtherEntityName` |
| Transliteration support | `Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName` |
| Legal jurisdiction | `Entity.LegalJurisdiction` |
| Legal address country | `Entity.LegalAddress.Country` |
| HQ country | `Entity.HeadquartersAddress.Country` |
| Entity type/context | `Entity.EntityCategory`, `Entity.LegalForm.EntityLegalFormCode` |

GLEIF defines Level 1 as the official identification data for each legal entity, including legal name, registered address, legal form, status, and LEI registration metadata.

---

## 2.2 Determine whether the LEI is “active”

There are two separate concepts:

| Concept | Field | Meaning |
|---|---|---|
| Is the legal entity still operating? | `Entity.EntityStatus` | Legal existence / operating state |
| Is the LEI record current or stale? | `Registration.RegistrationStatus` | LEI registration lifecycle |

### Recommended inclusion rule for this project

Use:

```text
Entity.EntityStatus = ACTIVE
AND Registration.RegistrationStatus IN (
  ISSUED,
  LAPSED,
  PENDING_TRANSFER,
  PENDING_ARCHIVAL
)
```

This aligns with GLEIF’s own **Active LEI** population definition in its statistics methodology. `LAPSED` means the entity is not known to have ceased operation, but the LEI record has not been renewed by its renewal date.

### Relevant Level 1 status values

#### `Entity.EntityStatus`

| Value | Use |
|---|---|
| `ACTIVE` | Legal entity reported as legally registered and operating |
| `INACTIVE` | Entity no longer operating / ceased / merged / invalid |
| `NULL` | Not applicable |

#### `Registration.RegistrationStatus`

| Value | Project relevance |
|---|---|
| `ISSUED` | Fully valid, issued LEI |
| `LAPSED` | LEI not renewed, but entity not known to be defunct |
| `PENDING_TRANSFER` | Still active during LOU transfer |
| `PENDING_ARCHIVAL` | Still active during transfer archival processing |
| `DUPLICATE` | Exclude |
| `RETIRED` | Exclude |
| `ANNULLED` | Exclude |
| `CANCELLED` | Exclude |
| `TRANSFERRED` | Not normally used in public current-state matching |
| `PENDING_VALIDATION` | Exclude from published operational set |

---

# 3. Level 2 parent relationships

## 3.1 How RR-CDF works

The **Level 2 Relationship Record RR-CDF** file stores directional relationships:

```text
StartNode = child entity
EndNode   = parent entity
```

For parent mapping:

| Relationship type | Meaning |
|---|---|
| `IS_DIRECTLY_CONSOLIDATED_BY` | Immediate accounting consolidating parent |
| `IS_ULTIMATELY_CONSOLIDATED_BY` | Highest-level accounting consolidating parent |

GLEIF defines these relationships using **accounting consolidation**, not simple equity ownership or brand hierarchy.

---

## 3.2 Required join path

### Child LEI → Parent LEI

```text
Level1.LEI
    =
RR.Relationship.StartNode.NodeID
```

Then extract:

```text
RR.Relationship.EndNode.NodeID
```

as the parent LEI.

Then join back to Level 1:

```text
RR.Relationship.EndNode.NodeID
    =
ParentLevel1.LEI
```

to retrieve:

```text
ParentLevel1.Entity.LegalName
```

---

## 3.3 Recommended RR filters

Use:

```text
Relationship.EndNode.NodeIDType = LEI
AND Relationship.RelationshipType IN (
  IS_DIRECTLY_CONSOLIDATED_BY,
  IS_ULTIMATELY_CONSOLIDATED_BY
)
AND Relationship.RelationshipStatus = ACTIVE
AND Registration.RegistrationStatus IN (
  PUBLISHED,
  LAPSED,
  PENDING_TRANSFER,
  PENDING_ARCHIVAL
)
```

This matches GLEIF’s own reporting logic for counting legal entities with reported direct and ultimate parents.

---

# 4. Required and useful columns by dataset

## 4.1 Internal company database

| Column | Purpose |
|---|---|
| `internal_company_id` | Stable internal key |
| `company_name` | Name to match against GLEIF |
| `normalized_company_name` | Matching support |
| `country` / `jurisdiction` | Match disambiguation |
| `internal_parent_id` or expected rollup | Validation / final mapping |
| Optional known identifiers | BIC, ISIN, registry ID, existing LEI if available |

---

## 4.2 GLEIF Level 1 LEI-CDF

### Required columns

| Field | Why needed |
|---|---|
| `LEI` | Primary key |
| `Entity.LegalName` | Primary matching target |
| `Entity.EntityStatus` | Filter to active legal entities |
| `Registration.RegistrationStatus` | Filter usable LEI registrations |

### Strongly recommended columns

| Field | Why useful |
|---|---|
| `Entity.OtherEntityNames.OtherEntityName` | Alternative company names |
| `Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName` | Non-Latin name matching |
| `Entity.LegalJurisdiction` | Match disambiguation |
| `Entity.LegalAddress.Country` | Match disambiguation |
| `Entity.HeadquartersAddress.Country` | Match disambiguation |
| `Entity.LegalForm.EntityLegalFormCode` | Normalize GmbH, Ltd, AG, Inc., etc. |
| `Registration.LastUpdateDate` | Recency marker |
| `Registration.NextRenewalDate` | Staleness marker |
| `Registration.ValidationSources` | Confidence/quality flag |
| `Registration.ValidationAuthority.ValidationAuthorityEntityID` | Possible registry linkage |

### Relevant Level 1 filter values

| Field | Relevant values |
|---|---|
| `Entity.EntityStatus` | `ACTIVE` |
| `Registration.RegistrationStatus` | `ISSUED`, `LAPSED`, `PENDING_TRANSFER`, `PENDING_ARCHIVAL` |
| `Registration.ValidationSources` | `FULLY_CORROBORATED`, `PARTIALLY_CORROBORATED`, `ENTITY_SUPPLIED_ONLY`, `PENDING` |

`FULLY_CORROBORATED` is the strongest validation source, while `ENTITY_SUPPLIED_ONLY` relies heavily on information provided by the registrant.

---

## 4.3 GLEIF Level 2 Relationship Record RR-CDF

### Required columns

| Field | Why needed |
|---|---|
| `Relationship.StartNode.NodeID` | Child LEI |
| `Relationship.StartNode.NodeIDType` | Confirm start node is an LEI |
| `Relationship.EndNode.NodeID` | Parent LEI |
| `Relationship.EndNode.NodeIDType` | Confirm parent has an LEI |
| `Relationship.RelationshipType` | Direct vs ultimate parent |
| `Relationship.RelationshipStatus` | Active vs inactive relationship |
| `Registration.RegistrationStatus` | Published/current relationship filter |

### Strongly recommended columns

| Field | Why useful |
|---|---|
| `Relationship.RelationshipPeriods.RelationshipPeriod.PeriodType` | Date context |
| `Relationship.RelationshipPeriods.RelationshipPeriod.StartDate` | Relationship effective date |
| `Relationship.RelationshipPeriods.RelationshipPeriod.EndDate` | Ended relationship detection |
| `Registration.LastUpdateDate` | Recency |
| `Registration.ValidationSources` | Relationship quality |
| `Registration.ValidationDocuments` | Evidence type |
| `Registration.ValidationReference` | Source reference if supplied |

### Relevant RR filter values

#### `Relationship.RelationshipType`

| Value | Use |
|---|---|
| `IS_DIRECTLY_CONSOLIDATED_BY` | Immediate parent |
| `IS_ULTIMATELY_CONSOLIDATED_BY` | Top-level parent |
| `IS_INTERNATIONAL_BRANCH_OF` | Branch/head office, not parent consolidation |
| `IS_FUND-MANAGED_BY` | Fund relationship, usually out of scope |
| `IS_SUBFUND_OF` | Fund relationship, usually out of scope |
| `IS_FEEDER_TO` | Fund relationship, usually out of scope |

#### `Relationship.RelationshipStatus`

| Value | Use |
|---|---|
| `ACTIVE` | Include |
| `INACTIVE` | Exclude |
| `NULL` | Exclude for active parent mapping |

#### `Registration.RegistrationStatus`

| Value | Use |
|---|---|
| `PUBLISHED` | Include |
| `LAPSED` | Include if following GLEIF active relationship convention |
| `PENDING_TRANSFER` | Include |
| `PENDING_ARCHIVAL` | Include |
| `RETIRED` | Exclude |
| `ANNULLED` | Exclude |
| `DUPLICATE` | Exclude |
| `PENDING_VALIDATION` | Exclude |
| `TRANSFERRED` | Not normally included in active public matching |

#### `Registration.ValidationSources`

| Value | Meaning |
|---|---|
| `FULLY_CORROBORATED` | Strongest evidence |
| `PARTIALLY_CORROBORATED` | Partially supported |
| `ENTITY_SUPPLIED_ONLY` | Mostly registrant-supplied |
| `PENDING` | Not publishable |

---

# 5. Important limitation: not every parent appears in RR-CDF

RR-CDF only contains a parent relationship when the relevant parent has an LEI or is represented in a relationship record structure.

If an entity does **not** report a public parent LEI, the reason may instead appear in the **Level 2 Reporting Exceptions** file.

Reporting exceptions cover cases such as:

| Reason | Meaning |
|---|---|
| `NO_LEI` | Parent does not have / consent to obtain an LEI |
| `NATURAL_PERSONS` | Controlled by natural persons |
| `NON_CONSOLIDATING` | No parent under the accounting consolidation definition |
| `NO_KNOWN_PERSON` | No known controlling person/entity |
| `NON_PUBLIC` | Parent information is non-public |

The two reporting categories are:

| Category |
|---|
| `DIRECT_ACCOUNTING_CONSOLIDATION_PARENT` |
| `ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT` |

Therefore:

```text
No RR-CDF parent row
≠
No parent exists
```

It may mean:

```text
Parent relationship is reported as an exception instead.
```

For completeness, the pipeline should check **Reporting Exceptions** when no matching parent RR record is found.

---

# 6. Recommended end-to-end process

## Step 1: Prepare internal names

Normalize:

- Case
- Punctuation
- Whitespace
- Common legal suffixes where appropriate
- Accent/transliteration variants

Retain original values for auditability.

---

## Step 2: Match internal names to GLEIF Level 1

Match against:

1. `Entity.LegalName`
2. `Entity.OtherEntityNames.OtherEntityName`
3. `Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName`

Use jurisdiction/country/legal form as tie-breakers.

Output:

```text
internal_company_id
matched_lei
matched_legal_name
match_score
match_basis
```

---

## Step 3: Keep in-scope active LEI records

Filter Level 1 to:

```text
EntityStatus = ACTIVE
RegistrationStatus IN (
  ISSUED,
  LAPSED,
  PENDING_TRANSFER,
  PENDING_ARCHIVAL
)
```

Retain `RegistrationStatus` in output as a quality field.

---

## Step 4: Link child LEI to parent LEI in RR-CDF

Join:

```text
matched_lei = RR.StartNode.NodeID
```

Filter RR to:

```text
EndNode.NodeIDType = LEI
RelationshipType IN (
  IS_DIRECTLY_CONSOLIDATED_BY,
  IS_ULTIMATELY_CONSOLIDATED_BY
)
RelationshipStatus = ACTIVE
RegistrationStatus IN (
  PUBLISHED,
  LAPSED,
  PENDING_TRANSFER,
  PENDING_ARCHIVAL
)
```

Output:

```text
child_lei
relationship_type
parent_lei
relationship_validation_source
relationship_registration_status
```

---

## Step 5: Resolve parent LEI back to parent name

Join:

```text
RR.EndNode.NodeID = Level1.LEI
```

Return:

```text
parent_lei
parent_legal_name
parent_entity_status
parent_registration_status
```

---

## Step 6: Match parent to internal company master

Match either:

1. **Parent LEI** directly, if internal data stores LEIs, or
2. **Parent legal name** back into the internal company database.

Output:

```text
internal_company_id
child_lei
child_legal_name
direct_parent_lei
direct_parent_name
ultimate_parent_lei
ultimate_parent_name
internal_parent_company_id
internal_parent_company_name
```

---

# 7. Recommended output model

| Field |
|---|
| `internal_company_id` |
| `internal_company_name` |
| `matched_child_lei` |
| `matched_child_legal_name` |
| `child_entity_status` |
| `child_registration_status` |
| `direct_parent_lei` |
| `direct_parent_legal_name` |
| `ultimate_parent_lei` |
| `ultimate_parent_legal_name` |
| `rr_validation_source_direct` |
| `rr_validation_source_ultimate` |
| `rr_registration_status_direct` |
| `rr_registration_status_ultimate` |
| `reporting_exception_direct` |
| `reporting_exception_ultimate` |
| `internal_parent_company_id` |
| `internal_parent_company_name` |
| `match_confidence` |

---

# 8. Key implementation decisions

## Recommended defaults

| Decision | Recommendation |
|---|---|
| Active LEI definition | `EntityStatus=ACTIVE` plus GLEIF active registration statuses |
| Parent relationship types | Use only direct and ultimate consolidation relationships |
| RR active filter | `RelationshipStatus=ACTIVE` |
| Parent ID requirement | Require `EndNode.NodeIDType=LEI` |
| Missing parent handling | Check Reporting Exceptions |
| Final internal rollup | Prefer LEI-to-LEI match; fall back to parent legal-name match |

---

# 9. GLEIF docs to review

- [GLEIF Level 1 Data: LEI-CDF Format 3.1](https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-1-data-lei-cdf-3-1-format)
- [GLEIF Level 2 Data: Relationship Record RR-CDF Format 2.1](https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-2-data-relationship-record-rr-cdf-2-1-format)
- [GLEIF Level 2 Data: Reporting Exceptions Format 2.1](https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-2-data-reporting-exceptions-2-1-format)
- [GLEIF Golden Copy and Delta Files](https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy)
- [GLEIF Data Dictionary](https://www.gleif.org/lei-data/access-and-use-lei-data/gleif-data-dictionary/2025-11-18_gleif-data-dictionary_v1.2_final.pdf)
- [GLEIF LEI Statistics](https://www.gleif.org/en/lei-data/global-lei-index/lei-statistics)
- [ROC Policy on Level 2 Parent Reporting](https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-2-data-who-owns-whom/roc-policy-on-level-2-data)
