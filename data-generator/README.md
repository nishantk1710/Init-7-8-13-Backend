# Data Generator - Simple Explanation

## What is this folder?

This folder creates **fake (synthetic) data** that looks like real SAP business data. It's used to test three major initiatives (I07, I08, I13) in the Spares AI platform.

---

## The Two Main Scripts

### 1. **cpi_discovery.py** - The Detective 🔍
**What it does:**
- Connects to the live SAP system (via CPI OData)
- Reads the actual data structure (what fields exist, their names, types)
- Saves this structure in the `discovery/` folder for later use

**When to run:**
- Only when SAP changes (new fields added, services updated)
- Needs credentials in `.env` file
- Requires internet connection

**Output files:**
- `metadata_*.xml` - Raw SAP data structure
- `properties.csv` - All field names and their types
- `entity_sets.csv` - List of all data tables
- `counts.csv` - How many records exist in SAP
- `value_domains.csv` - Full value breakdown for fields business rules depend on (e.g. `Dismm`, `Mstae`) — a real scan of every row, not a guess at which values exist

---

### 2. **generate.py** - The Data Factory 🏭
**What it does:**
- Reads the structure from `discovery/properties.csv`
- **Creates 2,000 realistic materials** and generates 24 months of fake transactions
- Creates synthetic SAP data for testing all 3 initiatives
- No credentials needed - completely offline

**Output files:**
All saved in `generated/` folder:
- **SAP data** (in `generated/sap/`) - Fake data shaped like real SAP:
  - Materials, vendors, purchase orders, goods receipts
  - Reservations, repairs, inventory movements
  - 18 different data files (CSV format)

- **Platform data** (in `generated/platform/`) - Data only our platform owns:
  - Inventory recommendations (I07)
  - Repair cases & attestations (I08)
  - Consumption plans & utilization status (I13)
  - Exceptions & approvals

**Configuration:**
```python
SEED = 42                    # Random seed (same seed = same data every time)
MATERIAL_COUNT = 2000        # Number of materials to create
HISTORY_MONTHS = 24          # How many months of history
AS_OF = date(2026, 9, 7)    # Today's date for the data
```

---

## The Three Initiatives (What the Data Supports)

### **Initiative 07 - Inventory Optimization**
- Tests recommending better safety stock levels
- Tracks demand patterns, lead times, criticality
- Data: Materials with high/low consumption, overstocked, understocked items

### **Initiative 08 - Repair Management**
- Tests tracking parts sent to vendors for repair
- Stages: removed → in-repair → overdue → back in stock
- Data: 80-series repairable materials with repair cases

### **Initiative 13 - Consumption Planning**
- Tests planning when materials will be used
- Materials ordered on-demand (OAR - Planned on Demand)
- Tracks: planned use date → actual use → overdue cases

---

## Discovery Folder Structure

```
discovery/
├── properties.csv          # All SAP fields (used by generate.py)
├── entity_sets.csv         # All data tables and their types
├── counts.csv              # How many records in each table
├── value_domains.csv       # Real value breakdown for business-rule fields (Dismm, Mstae, ...)
├── metadata_*.xml          # Raw SAP structure files
└── fr9_check.txt          # Special check for change documents
```

---

## Generated Folder Structure

```
generated/
├── sap/                    # Fake data like SAP provides
│   ├── MaterialSet.csv
│   ├── PurchaseOrderSet.csv
│   ├── MaterialDocumentHeaderSet.csv
│   └── ... (18 files total)
│
└── platform/               # Our platform's own data
    ├── inventory_recommendations.csv
    ├── repair_cases.csv
    ├── consumption_plans.csv
    └── ... (7 files total)
```

---

## Key Concepts in the Data

### **Material Stories** (Makes data realistic)
- `HIGH_CONSUMPTION` - Materials used regularly (I07 focus)
- `INTERMITTENT` - Used sporadically (unpredictable)
- `SLOW_MOVING` - Rarely used (aging stock)
- `OVERSTOCKED` - Too much inventory
- `UNDERSTOCKED_CRITICAL` - Not enough critical parts
- `LONG_LEAD` - Takes long to arrive (supplier delays)
- `OAR` - Ordered on-demand (I13)
- `REPAIRABLE` - Can be sent for repair (I08)
- `OBSOLETE` - No longer used

### **Criticality Levels**
- `CRITICAL` - Must not run out (high service level)
- `IMPACT` - Impacts production
- `INSURANCE` - Nice to have backup
- `NORMAL` - Standard part
- `OBSOLETE` - Can retire

---

## How to Use It

### Step 1: Update SAP structure (only if SAP changes)
```bash
python cpi_discovery.py --out discovery
```
Needs: `.env` file with CPI credentials

### Step 2: Generate synthetic data (do this regularly)
```bash
python generate.py
```
Needs: Nothing (uses `discovery/properties.csv` from step 1)

### Output
- 2,000 materials with realistic demand patterns
- 24 months of purchase orders, receipts, repairs
- Ready to test all three initiatives
- Deterministic (same seed = identical data each time)

---

## Why This Approach?

✅ **Realistic** - Based on actual SAP data structure  
✅ **Flexible** - Change SEED or MATERIAL_COUNT to get different datasets  
✅ **Fast** - Runs offline after discovery  
✅ **Repeatable** - Same seed always produces same data  
✅ **Safe** - No real customer data used  
✅ **Comprehensive** - Covers all 3 initiatives in one dataset  

---

## TL;DR
- `cpi_discovery.py` = Learns SAP structure (runs once, needs credentials)
- `generate.py` = Creates fake data (runs often, no credentials needed)
- Output = 2,000 materials with 24 months of realistic transactions
- Used to test inventory, repair, and consumption planning features
