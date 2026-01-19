# 🔍 DAO Governance Proposal Audit System

Automated detection of malicious code in DAO proposals - Identifies potential security risks by comparing proposal text intent with on-chain execution traces.

## Core Features

1. **Proposal Collection**: Collect DAO proposal data from on-chain
2. **Simulation Execution**: Execute proposals in Anvil Fork environment, capturing complete call traces
3. **Graph Construction**: Convert execution traces into NetworkX graph structures
4. **Intelligent Auditing**: Use LLM to compare graph structure with text intent, generating security reports

## Quick Start

### 1. Install Dependencies

**Install Foundry (Anvil)**:
```bash
# Windows (WSL)
wsl curl -L https://foundry.paradigm.xyz | bash
wsl foundryup

# Linux/macOS
curl -L https://foundry.paradigm.xyz | bash
foundryup
```

**Install Python Dependencies**:
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy `env.template` to `.env` and configure:

```bash
# RPC Configuration (Required)
ARBITRUM_RPC_URL=https://arb-mainnet.g.alchemy.com/v2/YOUR_API_KEY
# or
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_API_KEY

# LLM API Configuration (Required)
ANTHROPIC_API_KEY=sk-ant-xxxxx

# Third-party Platform (Optional)
ANTHROPIC_BASE_URL=https://your-third-party-api.com
LLM_MODEL=claude-sonnet-4-5-20250929

# Optional Configuration
ANVIL_PORT=8545
WSL_DISTRO=Ubuntu  # Windows users
```

### 3. Switch Chain Configuration

The system supports analyzing DAO proposals on different chains. When switching chains, modify the following configurations:

#### 3.1 Modify Governor Contract Address

Edit `src/parser/collector.py`, modify `GOVERNOR_ADDRESS`:

```python
# Lines 70-74
class ProposalCollector:
    # DAO Governor contract address
    # Compound Governor Bravo: 0xc0Da02939E1441F497fd74F78cE7Decb17B66529
    # Arbitrum Governor: 0xf07DeD9dC292157749B6Fd268E37DF6EA38395B9
    # Uniswap Governor: 0x408ED6354d4973f66138C91495F2f2FCbd8724C3
    GOVERNOR_ADDRESS = "0x408ED6354d4973f66138C91495F2f2FCbd8724C3"  # Modify to the corresponding chain's Governor address
```

#### 3.2 Modify Chain Identifier

In the `extract_proposal_from_event()` method of `src/parser/collector.py`, modify the chain identifier:

```python
# Line 198
"chain": "ethereum",  # Modify to the corresponding chain name: ethereum, arbitrum, optimism, polygon, etc.
```

#### 3.3 Configure RPC URL

Configure the corresponding chain's RPC URL in the `.env` file (default uses Arbitrum):

**Ethereum Mainnet (Uniswap, etc.)**:
```bash
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

**Arbitrum**:
```bash
ARBITRUM_RPC_URL=https://arb-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

#### 3.4 Known Chain Configuration Examples

**Arbitrum DAO**:
```python
# collector.py
GOVERNOR_ADDRESS = "0xf07DeD9dC292157749B6Fd268E37DF6EA38395B9"
"chain": "arbitrum"

# .env
ARBITRUM_RPC_URL=https://arb-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

**Uniswap DAO (Ethereum Mainnet)**:
```python
# collector.py
GOVERNOR_ADDRESS = "0x408ED6354d4973f66138C91495F2f2FCbd8724C3"
"chain": "ethereum"

# .env
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

#### 3.5 Modify Proposal ID and Block Range

In the `main()` function of `src/parser/collector.py` (around lines 327-341), modify the proposal ID and block range:

```python
def main():
    # ... existing code ...
    
    # Modify to the corresponding chain's proposal ID and block range
    proposal_id = YOUR_PROPOSAL_ID
    proposal_data = collector.collect_one(
        proposal_id=proposal_id,
        from_block=YOUR_PROPOSAL_BLOCK - 10,  # Near the proposal creation block number
        to_block=YOUR_PROPOSAL_BLOCK + 10
    )
```

#### 3.6 Add Known Contracts (Optional)

If better contract identification is needed, you can add known contract addresses for the corresponding chain in the `KNOWN_CONTRACTS` dictionary in `src/graph/graph_builder.py`:

```python
KNOWN_CONTRACTS = {
    # ... existing contracts ...
    
    # Uniswap related
    "0x408ed6354d4973f66138c91495f2f2fcbd8724c3": "Uniswap Governor",
    
    # Contracts on other chains...
}
```

**Note**:
- Ensure RPC URL matches the chain (Ethereum mainnet uses `MAINNET_RPC_URL`, Arbitrum uses `ARBITRUM_RPC_URL`)
- The system automatically selects the corresponding RPC URL based on environment variables (prioritizes chain-specific URLs like `ARBITRUM_RPC_URL`, otherwise uses `MAINNET_RPC_URL`)
- All chains use the same Compound Governor Bravo ABI, no modification needed

### 4. Run Analysis

```bash
# Step 1: Collect proposal
python -m src.parser.collector

# Step 2: Simulate execution
python -m src.simulator.simulator

# Step 3: Build graph
# Default uses data/traces/trace_report.json (replay_transaction mode)
python -m src.graph.graph_builder

# If using simulate_proposal mode, specify trace_summary_{proposal_id}.json
python -m src.graph.graph_builder --input data/traces/trace_summary_25.json

# Step 4: Generate audit report
python -m src.auditor.auditor
```

### 5. View Results

```bash
# View audit report
cat outputs/reports/audit_report.md

# View graph description
cat outputs/graph_description.txt
```

## Project Structure

```
src/
├── parser/          # Proposal collector
│   └── collector.py
├── simulator/       # Simulation execution engine
│   └── simulator.py
├── graph/           # Graph construction engine
│   └── graph_builder.py
└── auditor/         # Audit core
    ├── auditor.py
    └── ablation_auditor.py  # Ablation experiment auditor
```

## Output Files

- `data/proposals/collected_proposal.json` - Collected proposal data
- `data/traces/trace_report.json` - Execution trace report
- `outputs/proposal_graph.gpickle` - Graph object
- `outputs/graph_description.txt` - Graph description text
- `outputs/reports/audit_report.md` - Security audit report

### File Format Description

**Trace File Format**:
- `trace_report.json`: Generated by `replay_transaction()`, contains `trace_summary` field
- `trace_summary_{proposal_id}.json`: Generated by `simulate_proposal()`, contains `summary` field
- Graph builder automatically compatible with both formats, no manual conversion needed

## Tech Stack

- **Web3.py** - Blockchain interaction
- **Foundry Anvil** - Local Fork simulation execution
- **NetworkX** - Graph construction and analysis
- **Claude/LLM** - Intelligent comparison analysis

## How It Works

1. **Collect Proposal**: Get DAO proposal's targets, values, calldatas from on-chain
2. **Simulate Execution**: Execute proposal in Anvil Fork environment, use `debug_traceTransaction` to capture complete call stack
3. **Build Graph**: Convert call traces into directed graph, extract depth, breadth, central nodes and other features
4. **Intelligent Audit**: Use LLM to compare proposal text description with actual execution traces, identify inconsistencies and potential risks

## FAQ

**Q: Anvil startup failed?**
- Windows users: Ensure WSL is installed, install Foundry in WSL
- Check if port 8545 is occupied

**Q: 401 Unauthorized error?**
- Check if `ANTHROPIC_API_KEY` in `.env` is correct
- If using third-party platform, ensure `ANTHROPIC_BASE_URL` is set

**Q: Cannot connect to RPC node?**
- Check if RPC URL in `.env` is correct
- Confirm network connection is normal
- Ensure RPC URL matches target chain (Ethereum mainnet uses `MAINNET_RPC_URL`, Arbitrum uses `ARBITRUM_RPC_URL`)

**Q: How to switch to other chains?**
- Refer to "Switch Chain Configuration" section (Section 3)
- Modify `GOVERNOR_ADDRESS` and chain identifier in `src/parser/collector.py`
- Configure corresponding chain's RPC URL in `.env`
- Modify proposal ID and block range in `main()` function
- Ensure RPC URL matches target chain (Ethereum mainnet uses `MAINNET_RPC_URL`, Arbitrum uses `ARBITRUM_RPC_URL`)

**Q: Graph builder cannot find trace file?**
- Default reads `data/traces/trace_report.json` (replay_transaction mode)
- If using simulate_proposal mode, will generate `trace_summary_{proposal_id}.json`
- Use `--input` parameter to specify file path:
  ```bash
  python -m src.graph.graph_builder --input data/traces/trace_summary_25.json
  ```
- Graph builder automatically compatible with both formats (`trace_summary` and `summary` fields)

## License

MIT License
