#!/usr/bin/env python3
"""
Auditor - 提案审计器

功能：
1. 读取提案文本和图描述
2. 使用 LLM 进行一致性分析
3. 生成格式化的审计报告
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

from loguru import logger
from dotenv import load_dotenv
import os

# 尝试导入 LLM 库
try:
    from langchain_anthropic import ChatAnthropic
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    logger.warning("LangChain not available, will use direct API calls")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.warning("requests not available")

# 加载环境变量
load_dotenv()


# 系统合约地址列表（常识检查）
SYSTEM_CONTRACTS = {
    # 以太坊预编译合约 (0x1-0x9)
    "0x0000000000000000000000000000000000000001": "Ethereum Precompile: ECRecover",
    "0x0000000000000000000000000000000000000002": "Ethereum Precompile: SHA256",
    "0x0000000000000000000000000000000000000003": "Ethereum Precompile: RIPEMD160",
    "0x0000000000000000000000000000000000000004": "Ethereum Precompile: Identity",
    "0x0000000000000000000000000000000000000005": "Ethereum Precompile: ModExp",
    "0x0000000000000000000000000000000000000006": "Ethereum Precompile: BN256Add",
    "0x0000000000000000000000000000000000000007": "Ethereum Precompile: BN256Mul",
    "0x0000000000000000000000000000000000000008": "Ethereum Precompile: BN256Pairing",
    "0x0000000000000000000000000000000000000009": "Ethereum Precompile: Blake2F",
    
    # Arbitrum 系统合约
    "0x0000000000000000000000000000000000000064": "Arbitrum System Contract: L1 ArbSys",
    "0x0000000000000000000000000000000000000065": "Arbitrum System Contract: L2 ArbSys",
    
    # 其他常见的系统合约地址（可根据需要扩展）
    # "0x...": "Description",
}

# 代理合约模式识别（通过函数签名）
PROXY_PATTERNS = [
    "delegatecall",
    "implementation",
    "upgradeTo",
    "upgradeToAndCall",
    "changeAdmin",
    "admin",
    "proxy",
]


def is_system_contract(address: str) -> bool:
    """
    检查地址是否为系统合约
    
    Args:
        address: 合约地址（小写或混合大小写）
        
    Returns:
        是否为系统合约
    """
    addr_lower = address.lower()
    
    # 检查预编译合约 (0x1-0x9)
    if addr_lower.startswith("0x000000000000000000000000000000000000000"):
        last_char = addr_lower[-1]
        if last_char in "123456789":
            return True
    
    # 检查系统合约列表
    if addr_lower in SYSTEM_CONTRACTS:
        return True
    
    return False


def get_system_contract_description(address: str) -> Optional[str]:
    """
    获取系统合约描述
    
    Args:
        address: 合约地址
        
    Returns:
        系统合约描述，如果不是系统合约则返回 None
    """
    return SYSTEM_CONTRACTS.get(address.lower())


class LLMClient:
    """LLM 客户端抽象类"""
    
    def call(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """
        调用 LLM API
        
        Args:
            prompt: 用户提示词
            system_prompt: 系统提示词（可选）
            
        Returns:
            LLM 响应文本
        """
        raise NotImplementedError


class AnthropicClient(LLMClient):
    """Anthropic Claude API 客户端"""
    
    def __init__(self, api_key: Optional[str] = None, model: str = "claude-3-5-sonnet-20241022", 
                 base_url: Optional[str] = None):
        """
        初始化 Anthropic 客户端
        
        Args:
            api_key: API Key（如果为 None，从环境变量读取）
            model: 模型名称
            base_url: 自定义 API 基础 URL（用于第三方平台）
        """
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model
        self.base_url = base_url or os.getenv("ANTHROPIC_BASE_URL")
        
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment variables")
        
        if LANGCHAIN_AVAILABLE and not self.base_url:
            # 使用 LangChain（标准 API）
            self.client = ChatAnthropic(
                anthropic_api_key=self.api_key,
                model_name=self.model,
                temperature=0.1,
                max_tokens=4096
            )
            self.use_langchain = True
        else:
            # 使用直接 API 调用（支持第三方平台）
            self.use_langchain = False
            if self.base_url:
                # 第三方平台：确保 URL 格式正确
                base = self.base_url.rstrip("/")
                # 如果 base_url 已经包含完整路径，直接使用；否则添加 /v1/messages
                if "/v1/messages" in base or "/messages" in base:
                    self.api_url = base
                else:
                    self.api_url = f"{base}/v1/messages"
            else:
                self.api_url = "https://api.anthropic.com/v1/messages"
    
    def call(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """调用 Claude API"""
        if self.use_langchain:
            messages = []
            if system_prompt:
                messages.append(SystemMessage(content=system_prompt))
            messages.append(HumanMessage(content=prompt))
            
            response = self.client.invoke(messages)
            return response.content
        else:
            # 直接 API 调用（支持第三方平台）
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            
            payload = {
                "model": self.model,
                "max_tokens": 4096,
                "temperature": 0.1,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            }
            
            if system_prompt:
                payload["system"] = system_prompt
            
            logger.debug(f"Calling Anthropic API: {self.api_url}")
            logger.debug(f"Headers: {list(headers.keys())}")
            
            try:
                response = requests.post(self.api_url, headers=headers, json=payload, timeout=60)
                response.raise_for_status()
                result = response.json()
                
                # 提取文本内容
                if "content" in result and len(result["content"]) > 0:
                    return result["content"][0].get("text", "")
                return ""
            except requests.exceptions.HTTPError as e:
                logger.error(f"HTTP Error: {e}")
                logger.error(f"Response status: {response.status_code}")
                logger.error(f"Response body: {response.text[:500]}")
                raise
            except Exception as e:
                logger.error(f"Error calling API: {e}")
                raise


class OpenAIClient(LLMClient):
    """OpenAI API 客户端（也支持兼容 OpenAI 格式的第三方平台）"""
    
    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4", 
                 base_url: Optional[str] = None):
        """
        初始化 OpenAI 客户端
        
        Args:
            api_key: API Key
            model: 模型名称
            base_url: 自定义 API 基础 URL（用于第三方平台）
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        
        if LANGCHAIN_AVAILABLE and not self.base_url:
            # 使用 LangChain（标准 API）
            self.client = ChatOpenAI(
                openai_api_key=self.api_key,
                model_name=self.model,
                temperature=0.1,
                max_tokens=4096
            )
            self.use_langchain = True
        else:
            # 使用直接 API 调用（支持第三方平台）
            self.use_langchain = False
            if self.base_url:
                # 第三方平台：确保 URL 格式正确
                base = self.base_url.rstrip("/")
                # 如果 base_url 已经包含完整路径，直接使用；否则添加 /chat/completions
                if "/chat/completions" in base or "/v1/chat/completions" in base:
                    self.api_url = base
                elif "/v1" in base:
                    self.api_url = f"{base}/chat/completions"
                else:
                    self.api_url = f"{base}/v1/chat/completions"
            else:
                self.api_url = "https://api.openai.com/v1/chat/completions"
    
    def call(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """调用 OpenAI API"""
        if self.use_langchain:
            messages = []
            if system_prompt:
                messages.append(SystemMessage(content=system_prompt))
            messages.append(HumanMessage(content=prompt))
            
            response = self.client.invoke(messages)
            return response.content
        else:
            # 直接 API 调用（支持第三方平台）
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 4096
            }
            
            response = requests.post(self.api_url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            
            if "choices" in result and len(result["choices"]) > 0:
                return result["choices"][0]["message"]["content"]
            return ""


class Auditor:
    """提案审计器"""
    
    def __init__(self, 
                 llm_client: Optional[LLMClient] = None,
                 llm_type: str = "anthropic",
                 api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 base_url: Optional[str] = None):
        """
        初始化审计器
        
        Args:
            llm_client: 自定义 LLM 客户端（如果提供，将使用此客户端）
            llm_type: LLM 类型 ("anthropic" 或 "openai")
            api_key: API Key（如果为 None，从环境变量读取）
            model: 模型名称（如果为 None，从环境变量读取）
            base_url: 自定义 API 基础 URL（用于第三方平台）
        """
        if llm_client:
            self.llm = llm_client
        else:
            if llm_type.lower() == "anthropic":
                model = model or os.getenv("LLM_MODEL", "claude-3-5-sonnet-20241022")
                self.llm = AnthropicClient(api_key=api_key, model=model, base_url=base_url)
            elif llm_type.lower() == "openai":
                model = model or os.getenv("LLM_MODEL", "gpt-4")
                self.llm = OpenAIClient(api_key=api_key, model=model, base_url=base_url)
            else:
                raise ValueError(f"Unsupported LLM type: {llm_type}")
    
    def load_proposal(self, proposal_path: str) -> Dict[str, Any]:
        """
        加载提案数据
        
        Args:
            proposal_path: 提案 JSON 文件路径
            
        Returns:
            提案数据字典
        """
        proposal_file = Path(proposal_path)
        if not proposal_file.exists():
            raise FileNotFoundError(f"Proposal file not found: {proposal_path}")
        
        logger.info(f"Loading proposal from {proposal_file}")
        with open(proposal_file, 'r', encoding='utf-8') as f:
            proposal_data = json.load(f)
        
        return proposal_data
    
    def load_graph_description(self, graph_desc_path: str) -> str:
        """
        加载图描述文本
        
        Args:
            graph_desc_path: 图描述文件路径
            
        Returns:
            图描述文本
        """
        desc_file = Path(graph_desc_path)
        if not desc_file.exists():
            raise FileNotFoundError(f"Graph description file not found: {graph_desc_path}")
        
        logger.info(f"Loading graph description from {desc_file}")
        with open(desc_file, 'r', encoding='utf-8') as f:
            description = f.read()
        
        return description
    
    def build_audit_prompt(self, proposal_description: str, graph_description: str) -> str:
        """
        构建审计 Prompt
        
        Args:
            proposal_description: 提案文本描述
            graph_description: 图描述文本
            
        Returns:
            完整的审计 Prompt
        """
        prompt = f"""你是一位专业的智能合约安全审计专家。请对以下 DAO 提案进行深度审计分析。

## 任务说明

你需要执行以下三个核心审计任务：

### 1. [Conflict Detection] 冲突检测（含常识检查）
检查实际执行的节点（合约地址）是否在提案文本中明确提到。

**重要：常识检查规则**
- 如果图中出现的地址属于以下类型，**不应视为未披露风险**，而应标记为 `SYSTEM_LEVEL_CALL`（系统级常规调用）：
  1. **以太坊预编译合约**：地址范围 0x1-0x9（如 0x0000000000000000000000000000000000000001）
  2. **L2 系统合约**：如 Arbitrum 的 0x64（L1 ArbSys）、0x65（L2 ArbSys）等
  3. **标准代理转发逻辑**：通过 DELEGATECALL 实现的代理模式（如 EIP-1967 代理、UUPS 代理等）

- **重点关注**：只有那些**非系统级**、且**未在文本中解释用途**的第三方地址，才应标记为 `UNACCOUNTED_CONTRACT`（未披露风险）。

**系统合约地址参考**：
- **以太坊预编译合约**（地址格式：0x000000000000000000000000000000000000000X，X=1-9）：
  - 0x1: ECRecover（椭圆曲线签名验证）
  - 0x2: SHA256（哈希计算）
  - 0x3: RIPEMD160（哈希计算）
  - 0x4: Identity（数据复制）
  - 0x5: ModExp（模幂运算）
  - 0x6-0x9: BN256 椭圆曲线运算、Blake2F 哈希
- **Arbitrum L2 系统合约**：
  - 0x64 (0x0000000000000000000000000000000000000064): L1 ArbSys（L1 系统调用接口）
  - 0x65 (0x0000000000000000000000000000000000000065): L2 ArbSys（L2 系统调用接口）
- **代理转发模式**：如果调用链中包含 DELEGATECALL 且目标地址是已知的代理实现合约（如 EIP-1967、UUPS 等标准代理），应视为系统级调用。

**判断标准**：
1. 如果地址匹配上述系统合约，标记为 `SYSTEM_LEVEL_CALL`，`is_system_contract: true`
2. 如果地址未在文本中提到，但属于代理转发逻辑（通过 DELEGATECALL 调用标准代理实现），标记为 `SYSTEM_LEVEL_CALL`
3. 只有那些**既不是系统合约，也不是标准代理模式，且未在文本中说明**的地址，才标记为 `UNACCOUNTED_CONTRACT`，`is_system_contract: false`

### 2. [Depth Analysis] 深度分析
如果提案文本声称是"简单更新"或"轻微修改"，但执行图的深度达到 4 或更高，请分析是否存在"恶意隐藏深度"的风险。评估实际执行复杂度是否与文本描述一致。

### 3. [Function Semantic Match] 函数语义匹配
检查图中执行的函数名（如 execute, upgradeTo, transfer 等）是否与提案文本所述的意图吻合。识别任何语义不一致或未公开的函数调用。

## 输入数据

### 提案文本描述：
```
{proposal_description}
```

### 执行图描述：
```
{graph_description}
```

## 输出要求

请以 JSON 格式输出审计结果，包含以下字段：

```json
{{
  "consistency_score": <1-10 的整数，10 表示完全一致，1 表示严重不一致>,
  "conflict_detection": {{
    "unaccounted_contracts": [
      {{
        "address": "<合约地址>",
        "risk_level": "<low|medium|high>",
        "description": "<为什么这个地址未在文本中提到，可能的风险>",
        "is_system_contract": <true|false>,
        "contract_type": "<SYSTEM_LEVEL_CALL|UNACCOUNTED_CONTRACT>"
      }}
    ],
    "system_level_calls": [
      {{
        "address": "<系统合约地址>",
        "type": "<预编译合约|L2系统合约|代理转发>",
        "description": "<系统合约的用途说明>"
      }}
    ],
    "mentioned_contracts": [
      "<在文本中明确提到的合约地址列表>"
    ]
  }},
  "depth_analysis": {{
    "claimed_complexity": "<文本中声称的复杂度描述>",
    "actual_depth": <实际图深度>,
    "depth_mismatch": <true|false>,
    "risk_assessment": "<如果存在深度不匹配，评估风险等级和原因>"
  }},
  "function_semantic_match": {{
    "matched_functions": [
      {{
        "function": "<函数名>",
        "description": "<与文本描述的匹配情况>"
      }}
    ],
    "unmatched_functions": [
      {{
        "function": "<函数名>",
        "description": "<为什么这个函数调用与文本描述不匹配>",
        "risk_level": "<low|medium|high>"
      }}
    ]
  }},
  "potential_risks": [
    {{
      "type": "<风险类型，如 UNACCOUNTED_CONTRACT, DEPTH_MISMATCH, FUNCTION_MISMATCH 等>",
      "severity": "<low|medium|high|critical>",
      "description": "<详细的风险描述>",
      "recommendation": "<建议的应对措施>"
    }}
  ],
  "security_conclusion": "<总体安全结论，包括是否建议通过此提案>",
  "summary": "<简要总结，2-3 句话>"
}}
```

请仔细分析，确保输出有效的 JSON 格式。"""
        
        return prompt
    
    def parse_llm_response(self, response_text: str) -> Dict[str, Any]:
        """
        解析 LLM 响应，提取 JSON
        
        Args:
            response_text: LLM 响应文本
            
        Returns:
            解析后的 JSON 字典
        """
        # 尝试提取 JSON（可能被 ```json ... ``` 包裹）
        json_match = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # 尝试提取 {...} 格式的 JSON
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                json_str = response_text
        
        try:
            result = json.loads(json_str)
            return result
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            logger.debug(f"Response text: {response_text[:500]}")
            # 返回一个默认结构
            return {
                "consistency_score": 5,
                "error": "Failed to parse LLM response",
                "raw_response": response_text[:1000]
            }
    
    def generate_markdown_report(self, audit_result: Dict[str, Any], 
                                 proposal_id: Optional[str] = None) -> str:
        """
        生成 Markdown 格式的审计报告
        
        Args:
            audit_result: 审计结果字典
            proposal_id: 提案 ID（可选）
            
        Returns:
            Markdown 格式的报告文本
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        report = f"""# DAO 提案审计报告

**生成时间**: {timestamp}  
**提案 ID**: {proposal_id or "N/A"}

---

## 📊 一致性评分

**评分**: **{audit_result.get('consistency_score', 'N/A')}/10**

{self._get_score_description(audit_result.get('consistency_score', 5))}

---

## 🔍 冲突检测 (Conflict Detection)

### 未公开的合约地址

"""
        
        # 系统级调用
        system_calls = audit_result.get("conflict_detection", {}).get("system_level_calls", [])
        if system_calls:
            report += "### 系统级常规调用\n\n"
            report += "以下地址属于系统级合约，属于正常调用，无需在提案文本中特别说明：\n\n"
            for call in system_calls:
                report += f"- ✅ **{call.get('address', 'N/A')}**\n"
                report += f"  - 类型: `{call.get('type', 'N/A')}`\n"
                report += f"  - 说明: {call.get('description', 'N/A')}\n\n"
        
        # 未披露的第三方地址（非系统级）
        unaccounted = audit_result.get("conflict_detection", {}).get("unaccounted_contracts", [])
        # 过滤掉系统合约
        non_system_unaccounted = [
            c for c in unaccounted 
            if not c.get("is_system_contract", False) and 
               c.get("contract_type") != "SYSTEM_LEVEL_CALL"
        ]
        
        if non_system_unaccounted:
            report += "### ⚠️ 未公开的第三方合约地址\n\n"
            report += "以下地址未在提案文本中明确提到，且不属于系统级合约，需要进一步审查：\n\n"
            for contract in non_system_unaccounted:
                risk_emoji = self._get_risk_emoji(contract.get("risk_level", "medium"))
                report += f"- {risk_emoji} **{contract.get('address', 'N/A')}**\n"
                report += f"  - 风险等级: `{contract.get('risk_level', 'medium').upper()}`\n"
                report += f"  - 说明: {contract.get('description', 'N/A')}\n\n"
        elif not system_calls:
            report += "✅ 未发现未公开的合约地址。\n\n"
        
        mentioned = audit_result.get("conflict_detection", {}).get("mentioned_contracts", [])
        if mentioned:
            report += "### 文本中明确提到的合约\n\n"
            for addr in mentioned:
                report += f"- `{addr}`\n"
            report += "\n"
        
        report += "---\n\n## 📏 深度分析 (Depth Analysis)\n\n"
        
        depth_analysis = audit_result.get("depth_analysis", {})
        claimed = depth_analysis.get("claimed_complexity", "N/A")
        actual_depth = depth_analysis.get("actual_depth", "N/A")
        mismatch = depth_analysis.get("depth_mismatch", False)
        
        report += f"- **文本声称的复杂度**: {claimed}\n"
        report += f"- **实际执行深度**: {actual_depth}\n"
        report += f"- **深度不匹配**: {'⚠️ 是' if mismatch else '✅ 否'}\n\n"
        
        if mismatch:
            risk_assessment = depth_analysis.get("risk_assessment", "N/A")
            report += f"**风险评估**: {risk_assessment}\n\n"
        
        report += "---\n\n## 🔗 函数语义匹配 (Function Semantic Match)\n\n"
        
        func_match = audit_result.get("function_semantic_match", {})
        
        matched = func_match.get("matched_functions", [])
        if matched:
            report += "### ✅ 匹配的函数\n\n"
            for func in matched:
                report += f"- **{func.get('function', 'N/A')}**: {func.get('description', 'N/A')}\n"
            report += "\n"
        
        unmatched = func_match.get("unmatched_functions", [])
        if unmatched:
            report += "### ⚠️ 不匹配的函数\n\n"
            for func in unmatched:
                risk_emoji = self._get_risk_emoji(func.get("risk_level", "medium"))
                report += f"- {risk_emoji} **{func.get('function', 'N/A')}**\n"
                report += f"  - 风险等级: `{func.get('risk_level', 'medium').upper()}`\n"
                report += f"  - 说明: {func.get('description', 'N/A')}\n\n"
        else:
            report += "✅ 所有函数调用与文本描述匹配。\n\n"
        
        report += "---\n\n## ⚠️ 潜在风险点\n\n"
        
        risks = audit_result.get("potential_risks", [])
        if risks:
            for i, risk in enumerate(risks, 1):
                severity_emoji = self._get_severity_emoji(risk.get("severity", "medium"))
                report += f"### {i}. {severity_emoji} {risk.get('type', 'UNKNOWN_RISK')}\n\n"
                report += f"- **严重程度**: `{risk.get('severity', 'medium').upper()}`\n"
                report += f"- **描述**: {risk.get('description', 'N/A')}\n"
                report += f"- **建议**: {risk.get('recommendation', 'N/A')}\n\n"
        else:
            report += "✅ 未发现明显的潜在风险。\n\n"
        
        report += "---\n\n## 🔒 安全结论\n\n"
        report += f"{audit_result.get('security_conclusion', 'N/A')}\n\n"
        
        report += "---\n\n## 📝 总结\n\n"
        report += f"{audit_result.get('summary', 'N/A')}\n\n"
        
        report += "---\n\n*本报告由 AI 自动生成，仅供参考。建议结合人工审计进行最终决策。*\n"
        
        return report
    
    def _get_score_description(self, score: int) -> str:
        """获取评分描述"""
        if score >= 9:
            return "✅ **优秀**: 提案文本与执行轨迹高度一致，无明显风险。"
        elif score >= 7:
            return "✅ **良好**: 提案文本与执行轨迹基本一致，存在少量可接受的差异。"
        elif score >= 5:
            return "⚠️ **中等**: 提案文本与执行轨迹存在一定差异，需要进一步审查。"
        elif score >= 3:
            return "⚠️ **较差**: 提案文本与执行轨迹存在明显差异，存在潜在风险。"
        else:
            return "❌ **严重**: 提案文本与执行轨迹严重不一致，存在高风险。"
    
    def _get_risk_emoji(self, risk_level: str) -> str:
        """获取风险等级 emoji"""
        level_map = {
            "low": "🟢",
            "medium": "🟡",
            "high": "🟠",
            "critical": "🔴"
        }
        return level_map.get(risk_level.lower(), "⚪")
    
    def _get_severity_emoji(self, severity: str) -> str:
        """获取严重程度 emoji"""
        return self._get_risk_emoji(severity)
    
    def audit(self,
              proposal_path: str = "data/proposals/collected_proposal.json",
              graph_desc_path: str = "outputs/graph_description.txt",
              output_path: str = "outputs/reports/audit_report.md") -> Dict[str, Any]:
        """
        执行完整的审计流程
        
        Args:
            proposal_path: 提案文件路径
            graph_desc_path: 图描述文件路径
            output_path: 输出报告路径
            
        Returns:
            审计结果字典
        """
        logger.info("Starting audit process")
        
        # 1. 加载数据
        proposal_data = self.load_proposal(proposal_path)
        proposal_description = proposal_data.get("description", "")
        proposal_id = str(proposal_data.get("id", "N/A"))
        
        graph_description = self.load_graph_description(graph_desc_path)
        
        # 2. 构建 Prompt
        prompt = self.build_audit_prompt(proposal_description, graph_description)
        
        # 3. 调用 LLM
        logger.info("Calling LLM for audit analysis")
        system_prompt = "你是一位专业的智能合约安全审计专家，擅长分析 DAO 提案的一致性和安全性。"
        
        try:
            response = self.llm.call(prompt, system_prompt=system_prompt)
            logger.info("LLM response received")
        except Exception as e:
            logger.error(f"Error calling LLM: {e}")
            raise
        
        # 4. 解析响应
        audit_result = self.parse_llm_response(response)
        audit_result["proposal_id"] = proposal_id
        
        # 5. 生成报告
        markdown_report = self.generate_markdown_report(audit_result, proposal_id)
        
        # 6. 保存报告
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Saving audit report to {output_file}")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(markdown_report)
        
        logger.info("Audit process completed")
        
        return audit_result


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="DAO 提案审计工具")
    parser.add_argument(
        "--proposal",
        type=str,
        default="data/proposals/collected_proposal.json",
        help="提案 JSON 文件路径"
    )
    parser.add_argument(
        "--graph-desc",
        type=str,
        default="outputs/graph_description.txt",
        help="图描述文件路径"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="outputs/reports/audit_report.md",
        help="输出报告路径"
    )
    parser.add_argument(
        "--llm-type",
        type=str,
        choices=["anthropic", "openai"],
        default="anthropic",
        help="LLM 类型"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API Key（如果不提供，从环境变量读取）"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="模型名称（如果不提供，从环境变量读取）"
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="自定义 API 基础 URL（用于第三方平台）"
    )
    
    args = parser.parse_args()
    
    # 创建审计器
    auditor = Auditor(
        llm_type=args.llm_type,
        api_key=args.api_key,
        model=args.model,
        base_url=args.base_url
    )
    
    # 执行审计
    result = auditor.audit(
        proposal_path=args.proposal,
        graph_desc_path=args.graph_desc,
        output_path=args.output
    )
    
    print(f"\nAudit completed!")
    print(f"Consistency score: {result.get('consistency_score', 'N/A')}/10")
    print(f"Report saved to: {args.output}")


if __name__ == "__main__":
    main()
