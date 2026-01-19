#!/usr/bin/env python3
"""
Ablation Auditor - 消融实验审计器

功能：
1. 组1：仅使用提案文本进行审计
2. 组2：使用提案文本 + 原始 JSON Trace 进行审计
3. 生成格式化的审计报告

这是消融实验版本，用于对比含图结构的审计效果。
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

from loguru import logger
from dotenv import load_dotenv
import os

# 导入基础审计器的 LLM 客户端
from .auditor import LLMClient, AnthropicClient, OpenAIClient

# 加载环境变量
load_dotenv()


class AblationAuditor:
    """消融实验审计器"""
    
    def __init__(self, 
                 llm_client: Optional[LLMClient] = None,
                 llm_type: str = "anthropic",
                 api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 base_url: Optional[str] = None):
        """
        初始化消融实验审计器
        
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
    
    def load_trace_report(self, trace_path: str) -> Dict[str, Any]:
        """
        加载原始 JSON Trace 数据
        
        Args:
            trace_path: trace_report.json 文件路径
            
        Returns:
            Trace 数据字典
        """
        trace_file = Path(trace_path)
        if not trace_file.exists():
            raise FileNotFoundError(f"Trace report not found: {trace_path}")
        
        logger.info(f"Loading trace report from {trace_file}")
        with open(trace_file, 'r', encoding='utf-8') as f:
            trace_data = json.load(f)
        
        return trace_data
    
    def format_trace_summary(self, trace_data: Dict[str, Any]) -> str:
        """
        格式化 Trace 摘要为可读文本（使用 trace_summary）
        
        注意：此方法用于格式化处理后的 trace_summary 数据。
        组1不使用任何 trace 数据，此方法仅在 format_full_trace() 的回退逻辑中使用。
        
        Args:
            trace_data: Trace 数据字典
            
        Returns:
            格式化的 Trace 摘要文本
        """
        trace_summary = trace_data.get("trace_summary", {})
        calls = trace_summary.get("calls", [])
        total_calls = trace_summary.get("total_calls", len(calls))
        max_depth = trace_summary.get("max_depth", 0)
        
        formatted = f"""## 执行轨迹摘要

- **总调用数**: {total_calls}
- **最大深度**: {max_depth}

### 调用列表

"""
        
        # 限制显示的调用数量（避免 prompt 过长）
        max_display_calls = 50
        display_calls = calls[:max_display_calls]
        
        for i, call in enumerate(display_calls, 1):
            call_type = call.get("type", "UNKNOWN")
            from_addr = call.get("from", "N/A")
            to_addr = call.get("to", "N/A")
            value = call.get("value", 0)
            depth = call.get("depth", 0)
            function = call.get("function_signature", call.get("function_selector", "unknown"))
            
            formatted += f"{i}. **{call_type}** (深度: {depth})\n"
            formatted += f"   - From: `{from_addr}`\n"
            formatted += f"   - To: `{to_addr}`\n"
            formatted += f"   - Value: {value} wei\n"
            formatted += f"   - Function: `{function}`\n\n"
        
        if len(calls) > max_display_calls:
            formatted += f"\n*（仅显示前 {max_display_calls} 个调用，共 {total_calls} 个调用）*\n"
        
        return formatted
    
    def format_full_trace(self, trace_data: Dict[str, Any]) -> str:
        """
        格式化完整 Trace 数据为可读文本（用于组2，使用完整的 trace_calls）
        
        Args:
            trace_data: Trace 数据字典
            
        Returns:
            格式化的完整 Trace 文本
        """
        # 获取完整的 trace_calls（原始 trace 数据）
        trace_calls = trace_data.get("trace_calls", [])
        
        if not trace_calls:
            # 如果没有 trace_calls，回退到 trace_summary
            logger.warning("trace_calls not found, falling back to trace_summary")
            return self.format_trace_summary(trace_data)
        
        # 获取交易信息
        original_tx = trace_data.get("original_transaction", {})
        replay_tx = trace_data.get("replay_transaction", {})
        fork_config = trace_data.get("fork_config", {})
        
        formatted = f"""## 完整执行轨迹（原始 Trace 数据）

### 交易信息
- **原始交易哈希**: {original_tx.get('hash', 'N/A')}
- **重放交易哈希**: {replay_tx.get('hash', 'N/A')}
- **Fork 区块号**: {fork_config.get('fork_block_number', 'N/A')}
- **原始区块号**: {fork_config.get('original_block_number', 'N/A')}

### Trace 调用列表（共 {len(trace_calls)} 个调用）

"""
        
        # 格式化每个 trace call
        for i, call in enumerate(trace_calls, 1):
            call_type = call.get("type", "UNKNOWN")
            from_addr = call.get("from", "N/A")
            to_addr = call.get("to", "N/A")
            value = call.get("value", "0")
            gas = call.get("gas", "N/A")
            gas_used = call.get("gasUsed", "N/A")
            input_data = call.get("input", "")
            output_data = call.get("output", "")
            
            # 尝试提取函数签名（如果有）
            function_info = ""
            if input_data and len(input_data) >= 10:
                function_selector = input_data[:10]
                # 尝试从 trace_summary 中匹配函数签名
                trace_summary = trace_data.get("trace_summary", {})
                summary_calls = trace_summary.get("calls", [])
                for summary_call in summary_calls:
                    if summary_call.get("to", "").lower() == to_addr.lower():
                        func_sig = summary_call.get("function_signature", summary_call.get("function_selector", ""))
                        if func_sig and func_sig != "unknown":
                            function_info = f" ({func_sig})"
                            break
                if not function_info:
                    function_info = f" (selector: {function_selector})"
            
            formatted += f"{i}. **{call_type}**\n"
            formatted += f"   - From: `{from_addr}`\n"
            formatted += f"   - To: `{to_addr}`\n"
            formatted += f"   - Value: {value} wei\n"
            if gas != "N/A":
                formatted += f"   - Gas: {gas}\n"
            if gas_used != "N/A":
                formatted += f"   - Gas Used: {gas_used}\n"
            if function_info:
                formatted += f"   - Function: `{function_info.strip(' ()')}`\n"
            if input_data and len(input_data) > 10:
                # 只显示 input 的前100个字符
                input_preview = input_data[:100] + "..." if len(input_data) > 100 else input_data
                formatted += f"   - Input: `{input_preview}`\n"
            if output_data:
                # 只显示 output 的前100个字符
                output_preview = output_data[:100] + "..." if len(output_data) > 100 else output_data
                formatted += f"   - Output: `{output_preview}`\n"
            
            # 如果有子调用（calls 字段）
            if "calls" in call and call["calls"]:
                formatted += f"   - 子调用数: {len(call['calls'])}\n"
            
            formatted += "\n"
        
        return formatted
    
    def build_audit_prompt_group1(self, proposal_description: str, proposal_data: Dict[str, Any]) -> str:
        """
        构建组1的审计 Prompt（仅提案文本）
        
        Args:
            proposal_description: 提案文本描述
            proposal_data: 提案数据
            
        Returns:
            完整的审计 Prompt
        """
        # 提取提案的技术细节
        targets = proposal_data.get("targets", [])
        values = proposal_data.get("values", [])
        calldatas = proposal_data.get("calldatas", [])
        
        technical_details = f"""
### 提案技术参数：
- **目标合约地址 (targets)**: {', '.join(targets) if targets else '无'}
- **ETH 转账金额 (values)**: {values}
- **函数调用数据 (calldatas)**: {len(calldatas)} 个调用
"""
        
        prompt = f"""你是一位专业的智能合约安全审计专家。请对以下 DAO 提案进行深度审计分析。

## 实验说明

**这是消融实验组1**：本次审计**仅使用提案文本和技术参数**，不包含执行轨迹信息。

## 任务说明

你需要执行以下核心审计任务：

### 1. [Text Analysis] 文本一致性分析
分析提案文本描述是否清晰、完整，是否存在模糊或可能误导的表述。

### 2. [Technical Parameter Review] 技术参数审查
审查提案中的技术参数（targets, values, calldatas）是否与文本描述一致：
- 检查目标合约地址是否在文本中明确提到
- 检查 ETH 转账金额是否与文本描述一致
- 检查是否存在未在文本中说明的合约调用

### 3. [Risk Assessment] 风险评估
基于提案文本和技术参数，识别潜在的安全风险：
- 未明确说明的合约调用
- 可能存在的权限提升风险
- 资金转移风险
- 系统升级风险

### 4. [Completeness Check] 完整性检查
评估提案文本是否提供了足够的信息供社区做出明智决策。

## 输入数据

### 提案文本描述：
```
{proposal_description}
```

{technical_details}

## 输出要求

请以 JSON 格式输出审计结果，包含以下字段：

```json
{{
  "consistency_score": <1-10 的整数，10 表示完全一致，1 表示严重不一致>,
  "text_analysis": {{
    "clarity_score": <1-10 的整数，文本清晰度评分>,
    "completeness_score": <1-10 的整数，文本完整性评分>,
    "issues": [
      {{
        "type": "<问题类型>",
        "severity": "<low|medium|high>",
        "description": "<问题描述>"
      }}
    ]
  }},
  "technical_parameter_review": {{
    "mentioned_contracts": [
      "<在文本中明确提到的合约地址列表>"
    ],
    "unmentioned_contracts": [
      {{
        "address": "<未在文本中提到的合约地址>",
        "risk_level": "<low|medium|high>",
        "description": "<风险评估>"
      }}
    ],
    "value_consistency": {{
      "is_consistent": <true|false>,
      "description": "<ETH 转账金额与文本描述的一致性分析>"
    }}
  }},
  "risk_assessment": {{
    "identified_risks": [
      {{
        "type": "<风险类型>",
        "severity": "<low|medium|high|critical>",
        "description": "<详细的风险描述>",
        "recommendation": "<建议的应对措施>"
      }}
    ],
    "overall_risk_level": "<low|medium|high|critical>"
  }},
  "completeness_check": {{
    "missing_information": [
      {{
        "type": "<缺失信息类型>",
        "importance": "<low|medium|high>",
        "description": "<缺失信息的描述>"
      }}
    ],
    "sufficient_for_decision": <true|false>,
    "recommendation": "<是否建议通过此提案>"
  }},
  "security_conclusion": "<总体安全结论，包括是否建议通过此提案>",
  "summary": "<简要总结，2-3 句话>",
  "limitations": "<由于未使用执行轨迹分析，本次审计的局限性说明>"
}}
```

请仔细分析，确保输出有效的 JSON 格式。"""
        
        return prompt
    
    def build_audit_prompt_group2(self, proposal_description: str, proposal_data: Dict[str, Any], 
                                 trace_data: Dict[str, Any]) -> str:
        """
        构建组2的审计 Prompt（提案文本 + 原始 JSON Trace）
        
        Args:
            proposal_description: 提案文本描述
            proposal_data: 提案数据
            trace_data: Trace 数据
            
        Returns:
            完整的审计 Prompt
        """
        # 提取提案的技术细节
        targets = proposal_data.get("targets", [])
        values = proposal_data.get("values", [])
        calldatas = proposal_data.get("calldatas", [])
        
        technical_details = f"""
### 提案技术参数：
- **目标合约地址 (targets)**: {', '.join(targets) if targets else '无'}
- **ETH 转账金额 (values)**: {values}
- **函数调用数据 (calldatas)**: {len(calldatas)} 个调用
"""
        
        # 格式化完整 Trace 数据（使用 trace_calls）
        trace_summary_text = self.format_full_trace(trace_data)
        
        # 提取完整的 Trace JSON（使用完整的 trace_calls）
        trace_calls = trace_data.get("trace_calls", [])
        if trace_calls:
            # 使用完整的 trace_calls
            trace_json = json.dumps({
                "trace_calls": trace_calls,
                "original_transaction": trace_data.get("original_transaction", {}),
                "replay_transaction": trace_data.get("replay_transaction", {}),
                "fork_config": trace_data.get("fork_config", {})
            }, indent=2, ensure_ascii=False)
        else:
            # 回退到 trace_summary
            logger.warning("trace_calls not found, using trace_summary instead")
            trace_summary = trace_data.get("trace_summary", {})
            trace_json = json.dumps(trace_summary, indent=2, ensure_ascii=False)
        
        prompt = f"""你是一位专业的智能合约安全审计专家。请对以下 DAO 提案进行深度审计分析。

## 实验说明

**这是消融实验组2**：本次审计使用**提案文本 + 完整原始 JSON Trace 数据（trace_calls）**，但不使用图结构分析。

**重要**：本组使用完整的原始 Trace 数据（trace_calls），包含所有调用的详细信息（input、output、gas 等），而不是处理后的 trace_summary。

## 任务说明

你需要执行以下核心审计任务：

### 1. [Conflict Detection] 冲突检测
对比提案文本描述与实际执行轨迹（Trace），检查：
- 实际执行的合约地址是否在提案文本中明确提到
- 是否存在未在文本中说明的合约调用
- 调用深度和复杂度是否与文本描述一致

**重要：常识检查规则**
- 如果 Trace 中出现的地址属于以下类型，**不应视为未披露风险**：
  1. **以太坊预编译合约**：地址范围 0x1-0x9
  2. **L2 系统合约**：如 Arbitrum 的 0x64（L1 ArbSys）、0x65（L2 ArbSys）等
  3. **标准代理转发逻辑**：通过 DELEGATECALL 实现的代理模式

### 2. [Depth Analysis] 深度分析
分析 Trace 中的调用深度：
- 如果提案文本声称是"简单更新"或"轻微修改"，但 Trace 显示深度达到 4 或更高，请分析是否存在"恶意隐藏深度"的风险
- 评估实际执行复杂度是否与文本描述一致

### 3. [Function Semantic Match] 函数语义匹配
检查 Trace 中执行的函数名是否与提案文本所述的意图吻合：
- 识别任何语义不一致或未公开的函数调用
- 检查函数调用的参数和返回值是否符合预期

### 4. [Risk Assessment] 风险评估
基于提案文本和 Trace 数据，识别潜在的安全风险。

## 输入数据

### 提案文本描述：
```
{proposal_description}
```

{technical_details}

{trace_summary_text}

### 完整原始 Trace JSON 数据（trace_calls）：
```json
{trace_json}
```

**注意**：这是完整的原始 Trace 数据（trace_calls），包含所有调用的详细信息，包括 input、output、gas 使用等。请仔细分析每个调用的完整上下文。

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
    "actual_depth": <实际 Trace 深度>,
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
  "summary": "<简要总结，2-3 句话>",
  "limitations": "<由于未使用图结构分析，本次审计的局限性说明>"
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
                                 proposal_id: Optional[str] = None,
                                 group: int = 1) -> str:
        """
        生成 Markdown 格式的审计报告
        
        Args:
            audit_result: 审计结果字典
            proposal_id: 提案 ID（可选）
            group: 实验组编号（1 或 2）
            
        Returns:
            Markdown 格式的报告文本
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        group_name = "组1：仅提案文本" if group == 1 else "组2：提案文本 + 原始 JSON Trace"
        
        report = f"""# DAO 提案审计报告（消融实验 {group_name}）

**生成时间**: {timestamp}  
**提案 ID**: {proposal_id or "N/A"}  
**实验类型**: 消融实验 {group_name}

---

## ⚠️ 实验说明

本报告是基于**消融实验组{group}**生成的审计报告。与标准审计流程（含图结构）不同，本次审计：
"""
        
        if group == 1:
            report += """- ❌ **未使用**执行轨迹信息
- ❌ **未使用**图结构分析
- ✅ **仅基于**提案文本和技术参数进行分析

**局限性**: 由于未分析实际执行轨迹，无法检测：
- 隐藏的深度调用链
- 未公开的函数调用
- 代理合约转发逻辑
- 实际执行复杂度
"""
        else:
            report += """- ✅ **使用**完整原始 JSON Trace 数据（trace_calls）
- ❌ **未使用**图结构分析
- ✅ **基于**提案文本和完整 Trace 数据进行分析

**数据说明**: 本组使用完整的原始 Trace 数据（trace_calls），包含所有调用的详细信息（input、output、gas 等），而不是处理后的 trace_summary。

**局限性**: 由于未使用图结构分析，可能无法：
- 直观地识别调用链的拓扑结构
- 快速识别中心节点和关键路径
- 利用图算法进行深度分析
"""
        
        report += "\n---\n\n## 📊 一致性评分\n\n"
        report += f"**评分**: **{audit_result.get('consistency_score', 'N/A')}/10**\n\n"
        report += f"{self._get_score_description(audit_result.get('consistency_score', 5))}\n\n"
        
        # 根据组别生成不同的报告内容
        if group == 1:
            # 组1的报告格式
            report += self._generate_group1_report_content(audit_result)
        else:
            # 组2的报告格式（类似标准审计，但不使用图结构）
            report += self._generate_group2_report_content(audit_result)
        
        limitations = audit_result.get("limitations", "")
        if limitations:
            report += "\n---\n\n## ⚠️ 审计局限性\n\n"
            report += f"{limitations}\n\n"
        
        report += "\n---\n\n"
        report += f"*本报告由 AI 自动生成（消融实验组{group}），仅供参考。建议结合标准审计流程（含图结构）进行最终决策。*\n"
        
        return report
    
    def _generate_group1_report_content(self, audit_result: Dict[str, Any]) -> str:
        """生成组1的报告内容"""
        content = "## 📝 文本分析 (Text Analysis)\n\n"
        
        text_analysis = audit_result.get("text_analysis", {})
        clarity_score = text_analysis.get("clarity_score", "N/A")
        completeness_score = text_analysis.get("completeness_score", "N/A")
        
        content += f"- **文本清晰度**: {clarity_score}/10\n"
        content += f"- **文本完整性**: {completeness_score}/10\n\n"
        
        issues = text_analysis.get("issues", [])
        if issues:
            content += "### 发现的问题\n\n"
            for issue in issues:
                severity_emoji = self._get_severity_emoji(issue.get("severity", "medium"))
                content += f"- {severity_emoji} **{issue.get('type', 'N/A')}**\n"
                content += f"  - 严重程度: `{issue.get('severity', 'medium').upper()}`\n"
                content += f"  - 描述: {issue.get('description', 'N/A')}\n\n"
        else:
            content += "✅ 未发现明显的文本问题。\n\n"
        
        content += "---\n\n## 🔧 技术参数审查 (Technical Parameter Review)\n\n"
        
        tech_review = audit_result.get("technical_parameter_review", {})
        
        mentioned = tech_review.get("mentioned_contracts", [])
        if mentioned:
            content += "### 文本中明确提到的合约\n\n"
            for addr in mentioned:
                content += f"- `{addr}`\n"
            content += "\n"
        
        unmentioned = tech_review.get("unmentioned_contracts", [])
        if unmentioned:
            content += "### ⚠️ 未在文本中提到的合约\n\n"
            for contract in unmentioned:
                risk_emoji = self._get_risk_emoji(contract.get("risk_level", "medium"))
                content += f"- {risk_emoji} **{contract.get('address', 'N/A')}**\n"
                content += f"  - 风险等级: `{contract.get('risk_level', 'medium').upper()}`\n"
                content += f"  - 说明: {contract.get('description', 'N/A')}\n\n"
        else:
            content += "✅ 所有合约地址都在文本中明确提到。\n\n"
        
        value_consistency = tech_review.get("value_consistency", {})
        if value_consistency:
            is_consistent = value_consistency.get("is_consistent", True)
            content += f"### ETH 转账金额一致性\n\n"
            content += f"- **一致性**: {'✅ 是' if is_consistent else '⚠️ 否'}\n"
            content += f"- **说明**: {value_consistency.get('description', 'N/A')}\n\n"
        
        content += "---\n\n## ⚠️ 风险评估 (Risk Assessment)\n\n"
        
        risk_assessment = audit_result.get("risk_assessment", {})
        overall_risk = risk_assessment.get("overall_risk_level", "medium")
        risk_emoji = self._get_risk_emoji(overall_risk)
        
        content += f"### 总体风险等级: {risk_emoji} **{overall_risk.upper()}**\n\n"
        
        identified_risks = risk_assessment.get("identified_risks", [])
        if identified_risks:
            for i, risk in enumerate(identified_risks, 1):
                severity_emoji = self._get_severity_emoji(risk.get("severity", "medium"))
                content += f"### {i}. {severity_emoji} {risk.get('type', 'UNKNOWN_RISK')}\n\n"
                content += f"- **严重程度**: `{risk.get('severity', 'medium').upper()}`\n"
                content += f"- **描述**: {risk.get('description', 'N/A')}\n"
                content += f"- **建议**: {risk.get('recommendation', 'N/A')}\n\n"
        else:
            content += "✅ 未发现明显的潜在风险。\n\n"
        
        content += "---\n\n## ✅ 完整性检查 (Completeness Check)\n\n"
        
        completeness = audit_result.get("completeness_check", {})
        sufficient = completeness.get("sufficient_for_decision", False)
        
        content += f"- **信息是否充分**: {'✅ 是' if sufficient else '⚠️ 否'}\n\n"
        
        missing_info = completeness.get("missing_information", [])
        if missing_info:
            content += "### 缺失的信息\n\n"
            for info in missing_info:
                importance_emoji = self._get_risk_emoji(info.get("importance", "medium"))
                content += f"- {importance_emoji} **{info.get('type', 'N/A')}**\n"
                content += f"  - 重要性: `{info.get('importance', 'medium').upper()}`\n"
                content += f"  - 描述: {info.get('description', 'N/A')}\n\n"
        else:
            content += "✅ 提案文本提供了充分的信息。\n\n"
        
        recommendation = completeness.get("recommendation", "N/A")
        content += f"### 建议\n\n{recommendation}\n\n"
        
        content += "---\n\n## 🔒 安全结论\n\n"
        content += f"{audit_result.get('security_conclusion', 'N/A')}\n\n"
        
        content += "---\n\n## 📝 总结\n\n"
        content += f"{audit_result.get('summary', 'N/A')}\n\n"
        
        return content
    
    def _generate_group2_report_content(self, audit_result: Dict[str, Any]) -> str:
        """生成组2的报告内容（类似标准审计格式）"""
        content = "## 🔍 冲突检测 (Conflict Detection)\n\n"
        
        conflict_detection = audit_result.get("conflict_detection", {})
        
        # 系统级调用
        system_calls = conflict_detection.get("system_level_calls", [])
        if system_calls:
            content += "### 系统级常规调用\n\n"
            content += "以下地址属于系统级合约，属于正常调用，无需在提案文本中特别说明：\n\n"
            for call in system_calls:
                content += f"- ✅ **{call.get('address', 'N/A')}**\n"
                content += f"  - 类型: `{call.get('type', 'N/A')}`\n"
                content += f"  - 说明: {call.get('description', 'N/A')}\n\n"
        
        # 未披露的第三方地址（非系统级）
        unaccounted = conflict_detection.get("unaccounted_contracts", [])
        non_system_unaccounted = [
            c for c in unaccounted 
            if not c.get("is_system_contract", False) and 
               c.get("contract_type") != "SYSTEM_LEVEL_CALL"
        ]
        
        if non_system_unaccounted:
            content += "### ⚠️ 未公开的第三方合约地址\n\n"
            content += "以下地址未在提案文本中明确提到，且不属于系统级合约，需要进一步审查：\n\n"
            for contract in non_system_unaccounted:
                risk_emoji = self._get_risk_emoji(contract.get("risk_level", "medium"))
                content += f"- {risk_emoji} **{contract.get('address', 'N/A')}**\n"
                content += f"  - 风险等级: `{contract.get('risk_level', 'medium').upper()}`\n"
                content += f"  - 说明: {contract.get('description', 'N/A')}\n\n"
        elif not system_calls:
            content += "✅ 未发现未公开的合约地址。\n\n"
        
        mentioned = conflict_detection.get("mentioned_contracts", [])
        if mentioned:
            content += "### 文本中明确提到的合约\n\n"
            for addr in mentioned:
                content += f"- `{addr}`\n"
            content += "\n"
        
        content += "---\n\n## 📏 深度分析 (Depth Analysis)\n\n"
        
        depth_analysis = audit_result.get("depth_analysis", {})
        claimed = depth_analysis.get("claimed_complexity", "N/A")
        actual_depth = depth_analysis.get("actual_depth", "N/A")
        mismatch = depth_analysis.get("depth_mismatch", False)
        
        content += f"- **文本声称的复杂度**: {claimed}\n"
        content += f"- **实际执行深度**: {actual_depth}\n"
        content += f"- **深度不匹配**: {'⚠️ 是' if mismatch else '✅ 否'}\n\n"
        
        if mismatch:
            risk_assessment = depth_analysis.get("risk_assessment", "N/A")
            content += f"**风险评估**: {risk_assessment}\n\n"
        
        content += "---\n\n## 🔗 函数语义匹配 (Function Semantic Match)\n\n"
        
        func_match = audit_result.get("function_semantic_match", {})
        
        matched = func_match.get("matched_functions", [])
        if matched:
            content += "### ✅ 匹配的函数\n\n"
            for func in matched:
                content += f"- **{func.get('function', 'N/A')}**: {func.get('description', 'N/A')}\n"
            content += "\n"
        
        unmatched = func_match.get("unmatched_functions", [])
        if unmatched:
            content += "### ⚠️ 不匹配的函数\n\n"
            for func in unmatched:
                risk_emoji = self._get_risk_emoji(func.get("risk_level", "medium"))
                content += f"- {risk_emoji} **{func.get('function', 'N/A')}**\n"
                content += f"  - 风险等级: `{func.get('risk_level', 'medium').upper()}`\n"
                content += f"  - 说明: {func.get('description', 'N/A')}\n\n"
        else:
            content += "✅ 所有函数调用与文本描述匹配。\n\n"
        
        content += "---\n\n## ⚠️ 潜在风险点\n\n"
        
        risks = audit_result.get("potential_risks", [])
        if risks:
            for i, risk in enumerate(risks, 1):
                severity_emoji = self._get_severity_emoji(risk.get("severity", "medium"))
                content += f"### {i}. {severity_emoji} {risk.get('type', 'UNKNOWN_RISK')}\n\n"
                content += f"- **严重程度**: `{risk.get('severity', 'medium').upper()}`\n"
                content += f"- **描述**: {risk.get('description', 'N/A')}\n"
                content += f"- **建议**: {risk.get('recommendation', 'N/A')}\n\n"
        else:
            content += "✅ 未发现明显的潜在风险。\n\n"
        
        content += "---\n\n## 🔒 安全结论\n\n"
        content += f"{audit_result.get('security_conclusion', 'N/A')}\n\n"
        
        content += "---\n\n## 📝 总结\n\n"
        content += f"{audit_result.get('summary', 'N/A')}\n\n"
        
        return content
    
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
    
    def audit_group1(self,
                     proposal_path: str = "data/proposals/collected_proposal.json",
                     output_path: str = "outputs/reports/ablation_group1_report.md") -> Dict[str, Any]:
        """
        执行组1的审计流程（仅提案文本）
        
        Args:
            proposal_path: 提案文件路径
            output_path: 输出报告路径
            
        Returns:
            审计结果字典
        """
        logger.info("Starting ablation audit group 1 (proposal text only)")
        
        # 1. 加载数据
        proposal_data = self.load_proposal(proposal_path)
        proposal_description = proposal_data.get("description", "")
        proposal_id = str(proposal_data.get("id", "N/A"))
        
        # 2. 构建 Prompt（组1）
        prompt = self.build_audit_prompt_group1(proposal_description, proposal_data)
        
        # 3. 调用 LLM
        logger.info("Calling LLM for ablation audit group 1")
        system_prompt = "你是一位专业的智能合约安全审计专家，擅长分析 DAO 提案的一致性和安全性。注意：这是一个消融实验组1，仅基于提案文本进行分析，不包含执行轨迹信息。"
        
        try:
            response = self.llm.call(prompt, system_prompt=system_prompt)
            logger.info("LLM response received")
        except Exception as e:
            logger.error(f"Error calling LLM: {e}")
            raise
        
        # 4. 解析响应
        audit_result = self.parse_llm_response(response)
        audit_result["proposal_id"] = proposal_id
        audit_result["experiment_type"] = "ablation_group1_text_only"
        
        # 5. 生成报告
        markdown_report = self.generate_markdown_report(audit_result, proposal_id, group=1)
        
        # 6. 保存报告
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Saving ablation audit group 1 report to {output_file}")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(markdown_report)
        
        logger.info("Ablation audit group 1 completed")
        
        return audit_result
    
    def audit_group2(self,
                     proposal_path: str = "data/proposals/collected_proposal.json",
                     trace_path: str = "data/traces/trace_report.json",
                     output_path: str = "outputs/reports/ablation_group2_report.md") -> Dict[str, Any]:
        """
        执行组2的审计流程（提案文本 + 原始 JSON Trace）
        
        Args:
            proposal_path: 提案文件路径
            trace_path: Trace 文件路径
            output_path: 输出报告路径
            
        Returns:
            审计结果字典
        """
        logger.info("Starting ablation audit group 2 (proposal text + raw JSON trace)")
        
        # 1. 加载数据
        proposal_data = self.load_proposal(proposal_path)
        proposal_description = proposal_data.get("description", "")
        proposal_id = str(proposal_data.get("id", "N/A"))
        
        trace_data = self.load_trace_report(trace_path)
        
        # 2. 构建 Prompt（组2）
        prompt = self.build_audit_prompt_group2(proposal_description, proposal_data, trace_data)
        
        # 3. 调用 LLM
        logger.info("Calling LLM for ablation audit group 2")
        system_prompt = "你是一位专业的智能合约安全审计专家，擅长分析 DAO 提案的一致性和安全性。注意：这是一个消融实验组2，使用提案文本和原始 JSON Trace 数据进行分析，但不使用图结构。"
        
        try:
            response = self.llm.call(prompt, system_prompt=system_prompt)
            logger.info("LLM response received")
        except Exception as e:
            logger.error(f"Error calling LLM: {e}")
            raise
        
        # 4. 解析响应
        audit_result = self.parse_llm_response(response)
        audit_result["proposal_id"] = proposal_id
        audit_result["experiment_type"] = "ablation_group2_text_trace"
        
        # 5. 生成报告
        markdown_report = self.generate_markdown_report(audit_result, proposal_id, group=2)
        
        # 6. 保存报告
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Saving ablation audit group 2 report to {output_file}")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(markdown_report)
        
        logger.info("Ablation audit group 2 completed")
        
        return audit_result


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="DAO 提案审计工具（消融实验）")
    parser.add_argument(
        "--group",
        type=int,
        choices=[1, 2],
        required=True,
        help="实验组编号：1=仅提案文本，2=提案文本+原始JSON Trace"
    )
    parser.add_argument(
        "--proposal",
        type=str,
        default="data/proposals/collected_proposal.json",
        help="提案 JSON 文件路径"
    )
    parser.add_argument(
        "--trace",
        type=str,
        default="data/traces/trace_report.json",
        help="Trace JSON 文件路径（组2需要）"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出报告路径（如果不提供，使用默认路径）"
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
    
    # 创建消融实验审计器
    auditor = AblationAuditor(
        llm_type=args.llm_type,
        api_key=args.api_key,
        model=args.model,
        base_url=args.base_url
    )
    
    # 根据组别执行审计
    if args.group == 1:
        output_path = args.output or "outputs/reports/ablation_group1_report.md"
        result = auditor.audit_group1(
            proposal_path=args.proposal,
            output_path=output_path
        )
        print(f"\n✅ 消融实验组1审计完成！")
        print(f"一致性评分: {result.get('consistency_score', 'N/A')}/10")
        print(f"报告已保存到: {output_path}")
        print(f"\n⚠️  注意：这是消融实验组1，仅使用提案文本。")
    else:
        output_path = args.output or "outputs/reports/ablation_group2_report.md"
        result = auditor.audit_group2(
            proposal_path=args.proposal,
            trace_path=args.trace,
            output_path=output_path
        )
        print(f"\n✅ 消融实验组2审计完成！")
        print(f"一致性评分: {result.get('consistency_score', 'N/A')}/10")
        print(f"报告已保存到: {output_path}")
        print(f"\n⚠️  注意：这是消融实验组2，使用提案文本和原始 JSON Trace，但不使用图结构。")


if __name__ == "__main__":
    main()
