# 📜 Bangladesh Laws (BDLaws) Dataset & Analytics Pipeline

Comprehensive data guide, download pipeline, and empirical statistical analysis for the **Bangladesh Legal Copilot** project.

---

## 📑 Table of Contents
- [1. Dataset Overview & Provenance](#1-dataset-overview--provenance)
- [2. Directory Structure](#2-directory-structure)
- [3. Dataset Download Pipeline](#3-dataset-download-pipeline)
- [4. Data Analytics Pipeline & Commands](#4-data-analytics-pipeline--commands)
- [5. Empirical Analytical Statistics](#5-empirical-analytical-statistics)
- [6. Field-by-Field Schema Profile](#6-field-by-field-schema-profile)
- [7. Key Insights & Implications for Legal RAG](#7-key-insights--implications-for-legal-rag)

---

## 1. Dataset Overview & Provenance

* **Source**: Official Bangladesh Laws Portal ([bdlaws.minlaw.gov.bd](http://bdlaws.minlaw.gov.bd/)), maintained by the Legislative and Parliamentary Affairs Division, Ministry of Law, Justice and Parliamentary Affairs, Government of the People's Republic of Bangladesh.
* **Hugging Face Repository**: [Ashik2380/bdlaws](https://huggingface.co/datasets/Ashik2380/bdlaws)
* **Dataset Shape**: Flat table formatted as a JSON Lines (`.jsonl`) file where each individual record represents a single **section** of an Act, with Act-level metadata (such as Act name, publication date, volume, and preamble) repeated across all its respective sections.
* **Coverage**: All 56 official legislative volumes spanning colonial regulations (from 1799) to contemporary parliamentary enactments (through 2024).

---

## 2. Directory Structure

```
data/
├── raw/                               # Raw datasets (excluded from git tracking)
│   └── bdlaws.jsonl                   # Full dataset (~563 MB, 43,071 records)
├── processed/                         # Processed data, samples, and analytical profiles
│   ├── sample.jsonl                   # 100-record benchmark sample (374 KB)
│   ├── sample_profile.csv             # Field frequency & quality profile for sample
│   ├── bdlaws_profile.csv             # Field frequency & quality profile for full dataset
│   └── analyze_sample.py              # Advanced multi-format data profiling script
├── download.py                        # Convenience runner for download pipeline
└── README.md                          # Data documentation & statistical report
```

> **Note on Version Control:** In accordance with `.gitignore`, `data/raw/bdlaws.jsonl` is excluded from git commits due to its 563 MB size, ensuring a clean and lightweight repository.

---

## 3. Dataset Download Pipeline

A reproducible, automated download utility is provided at `scripts/download_dataset.py` (with a shortcut at `data/download.py`). It features resumable chunked streaming, real-time speed/ETA metrics, and post-download SHA/JSON integrity validation.

### Automated Download Command

From the repository root:
```bash
python scripts/download_dataset.py
```

Or from within the `data/` folder:
```bash
python download.py
```

### CLI Options
* `--output <path>`: Specify custom destination path (default: `data/raw/bdlaws.jsonl`).
* `--force`: Force re-download and overwrite existing file.
* `--verify-only`: Verify file integrity and validate JSON records without downloading.

```bash
# Example: verify existing downloaded file
python scripts/download_dataset.py --verify-only
```

---

## 4. Data Analytics Pipeline & Commands

The dataset profiling engine [analyze_sample.py](processed/analyze_sample.py) performs automated schema inspection, completeness and quality scoring, legal domain entity extraction, and language distribution profiling across JSON, JSONL, and concatenated JSON objects.

### Run Analytics on the Sample Dataset (100 records)
```bash
cd data/processed
python analyze_sample.py sample.jsonl --csv
```

### Run Analytics on the Full Dataset (43,071 records)
```bash
cd data/processed
python analyze_sample.py ../raw/bdlaws.jsonl --csv --out-dir .
```

---

## 5. Empirical Analytical Statistics

Below is the comparative breakdown between the **Benchmark Sample (`sample.jsonl`)** and the **Full National Dataset (`bdlaws.jsonl`)**:

| Metric | Sample Dataset (`sample.jsonl`) | Full Dataset (`bdlaws.jsonl`) | Delta / Notes |
| :--- | :--- | :--- | :--- |
| **Total Records / Sections** | 100 | **43,071** | Complete legislative body |
| **File Size on Disk** | 374 KB | **563.0 MB** | 590,377,244 bytes |
| **Data Format Detected** | Concatenated JSON objects | **Strict JSONL** (line-by-line) | Both supported seamlessly |
| **Unique Legislative Acts** | 10 | **1,467** | All 56 Volumes |
| **Active Enactments (In-force)**| 10 (100.0%) | **1,264 (86.16%)** | Active laws |
| **Repealed Enactments** | 0 (0.00%) | **203 (13.84%)** | Repealed Acts |
| **Active Sections** | 100 (100.0%) | **35,409 (82.21%)** | Active legal provisions |
| **Repealed Sections** | 0 (0.00%) | **7,662 (17.79%)** | Repealed provisions |
| **Official Volumes Represented**| 1 | **56** | Vol 1 to Vol 56 |
| **Overall Completeness Score** | 91.88% | **88.62%** | Populated vs total cells |

### Language Distribution (`section_text`)

| Language | Sample Dataset Count | Full Dataset Count | Percentage of Full Laws |
| :--- | :--- | :--- | :--- |
| **Bangla (বাংলা)** | 0 (0.00%) | **22,138** | **51.40%** |
| **English** | 100 (100.00%) | **20,188** | **46.87%** |
| **Bilingual / Mixed** | 0 (0.00%) | **674** | **1.56%** |
| **Empty / Repealed Placeholder** | 0 (0.00%) | **71** | **0.16%** |

---

## 6. Field-by-Field Schema Profile

Analysis of all 17 fields across 43,071 records in `bdlaws.jsonl`:

| Field Name | Type | Present % | Quality Score | Distinct Values | Description / Quality Observation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `id` | `int` | 100.00% | 100.0% | 43,071 | Unique section identifier. |
| `source_file` | `str` | 100.00% | 100.0% | 2 | Scraper volume batch identifier. |
| `volume_name` | `str` | 100.00% | 100.0% | 56 | Official volume name (e.g. Volume 1 to 56). |
| `act_name` | `str` | 100.00% | 100.0% | 1,467 | Formal title of the Act/Ordinance. |
| `act_no` | `str` | 100.00% | 99.6% | 233 | Official act/ordinance number. |
| `published_date` | `str` | 100.00% | 98.6% | 953 | Date of enactment/publication. |
| `repealed` | `bool` | 100.00% | 100.0% | 2 (`True`/`False`) | Repeal status flag. |
| `repealed_notice` | `str` | 100.00% | 17.8% | 186 | Notice of repealing statute (populated when `repealed=True`). |
| `short_title` | `str` | 100.00% | 97.5% | 1,412 | Short title or subject summary of Act. |
| `preamble` | `str` | 100.00% | 97.5% | 1,406 | Formal legislative preamble / statement of purpose. |
| `num_of_sections` | `int` | 100.00% | 100.0% | 143 | Total section count declared for the Act. |
| `section_id` | `int` | 100.00% | 100.0% | 585 | Section ordinal number within the Act. |
| `section_name` | `str` | 100.00% | 100.0% | 34,349 | Section heading / marginal note. |
| `section_text` | `str` | 100.00% | 100.0% | 41,936 | Substantive statutory legal provisions. |
| `related_act` | `list` | 100.00% | 100.0% | — | Related Act IDs. |
| `section_related_acts` | `list` | 100.00% | 34.8% | — | Section-level cross-references (sparse). |
| `notes` | `list` | 100.00% | 60.7% | — | Amendment footnotes and gazette citations. |

---

## 7. Key Insights & Implications for Legal RAG

### 1. Linguistic Bilinguality (51.4% Bangla vs 46.9% English)
* **Observation**: Bangladesh's legal corpus reflects its historical evolution. Pre-1971 colonial and Pakistan-era enactments remain in English, whereas post-1987 laws (following the *Bangla Bhasha Procholon Ain, 1987*) are enacted predominantly in Bangla.
* **RAG Impact**: The retrieval system cannot rely solely on monolingual English or monolingual Bangla embeddings. As implemented in `apps/rag-api/kb_json.py` and `rag_pdf.py`, multilingual embeddings (`paraphrase-multilingual-mpnet-base-v2` or `all-MiniLM-L6-v2`) and cross-lingual query expansion (Bangla ↔ English) are essential.

### 2. High Proportion of Repealed Legislation (17.8% Sections)
* **Observation**: 7,662 sections across 203 Acts are marked as `repealed = True` (with explanations in `repealed_notice`).
* **RAG Impact**: A legal copilot must not present repealed statutes as active law. RAG chunking headers must inject `[STATUS: REPEALED]` metadata into the indexed embeddings, and the retrieval pipeline must support explicit filtering via `/repealed` commands.

### 3. Sample Bias in `sample.jsonl`
* **Observation**: `sample.jsonl` contains only 100 records from Volume 1 (1799–1850). All 100 records are in English and none are repealed.
* **RAG Impact**: Testing only against `sample.jsonl` will fail to test Bangla parsing, Unicode digit normalization (`০-৯` to `0-9`), and repealed notices. Full validation must be executed against `bdlaws.jsonl`.

### 4. Top 5 Acts by Section Density
1. **The Code of Criminal Procedure, 1898** (CrPC) — 585 sections
2. **The Penal Code, 1860** — 583 sections
3. **The Bangladesh Merchant Shipping Ordinance, 1983** — 516 sections
4. **কোম্পানী আইন, ১৯৯৪ (Companies Act, 1994)** — 418 sections
5. **The Succession Act, 1925** — 395 sections

These 5 major acts alone represent over 2,497 sections and form the foundation of core civil, criminal, and commercial law queries.
