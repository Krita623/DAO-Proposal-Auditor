#!/usr/bin/env python3
"""
Proposal Simulator - 提案模拟执行器

功能：
1. 启动本地 Anvil Fork 环境
2. 模拟执行提案的 calldata
3. 捕获交易的执行轨迹（Trace）
4. 提取跨合约调用和资金转移信息
5. 生成结构化的 trace_summary.json
"""

import json
import os
import subprocess
import time
import signal
import platform
import sys
import requests
from typing import Dict, List, Optional, Any
from pathlib import Path
from web3 import Web3
from web3.exceptions import TransactionNotFound, BlockNotFound
from dotenv import load_dotenv
from loguru import logger

# 加载环境变量
load_dotenv()


# 常用函数签名字典（内置常用函数）
COMMON_FUNCTION_SIGNATURES = {
    "0xa9059cbb": "transfer(address,uint256)",
    "0x23b872dd": "transferFrom(address,address,uint256)",
    "0x095ea7b3": "approve(address,uint256)",
    "0x40c10f19": "mint(address,uint256)",
    "0x42966c68": "burn(uint256)",
    "0xbc86e06b": "execute(address,uint256,bytes)",
    "0x5c60da1b": "implementation()",
    "0x8da5cb5b": "owner()",
    "0x715018a6": "renounceOwnership()",
    "0xf2fde38b": "transferOwnership(address)",
    "0x70a08231": "balanceOf(address)",
    "0x18160ddd": "totalSupply()",
    "0x06fdde03": "name()",
    "0x95d89b41": "symbol()",
    "0x313ce567": "decimals()",
    "0xdd62ed3e": "allowance(address,address)",
    "0x4e71e0c8": "claim()",
    "0x379607f5": "claimable(address)",
    "0x2e1a7d4d": "withdraw(uint256)",
    "0x3d18b912": "getReward()",
    "0x5c975abb": "paused()",
    "0x8456cb59": "pause()",
    "0x3f4ba83a": "unpause()",
}


def convert_to_serializable(obj: Any) -> Any:
    """
    将 Web3 的 AttributeDict 等对象转换为可 JSON 序列化的格式
    
    Args:
        obj: 需要转换的对象
        
    Returns:
        可序列化的对象（dict, list, str, int, float, bool, None）
    """
    from web3.datastructures import AttributeDict
    
    if isinstance(obj, AttributeDict):
        return {k: convert_to_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, dict):
        return {k: convert_to_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [convert_to_serializable(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    elif hasattr(obj, '__dict__'):
        return convert_to_serializable(obj.__dict__)
    else:
        # 对于其他类型，尝试转换为字符串
        return str(obj)


def resolve_function_signature(function_selector: str) -> str:
    """
    解析函数选择器，返回可读的函数签名
    
    Args:
        function_selector: 函数选择器（4字节，如 "0xa9059cbb"）
        
    Returns:
        函数签名（如 "transfer(address,uint256)"），如果无法解析则返回选择器本身
    """
    if not function_selector or function_selector == "0x" or len(function_selector) < 10:
        return function_selector
    
    # 标准化选择器格式（确保是 0x + 8 个十六进制字符）
    selector = function_selector[:10].lower()
    
    # 首先检查内置字典
    if selector in COMMON_FUNCTION_SIGNATURES:
        return COMMON_FUNCTION_SIGNATURES[selector]
    
    # 如果内置字典中没有，尝试调用 4byte.directory API
    try:
        # 移除 0x 前缀
        selector_hex = selector[2:] if selector.startswith("0x") else selector
        
        # 调用 4byte.directory API
        api_url = f"https://www.4byte.directory/api/v1/signatures/?hex_signature={selector_hex}"
        response = requests.get(api_url, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("count", 0) > 0:
                # 返回第一个匹配的结果
                results = data.get("results", [])
                if results:
                    return results[0].get("text_signature", selector)
    except Exception as e:
        # 如果 API 调用失败，静默失败，返回选择器
        logger.debug(f"无法从 4byte.directory 解析函数签名 {selector}: {e}")
    
    # 如果都无法解析，返回原始选择器
    return selector


class ProposalSimulator:
    """提案模拟执行器"""
    
    def __init__(
        self,
        rpc_url: Optional[str] = None,
        anvil_port: int = 8545,
        fork_block: Optional[int] = None,
        use_wsl: Optional[bool] = None
    ):
        """
        初始化模拟器
        
        Args:
            rpc_url: 主网 RPC URL（用于 Fork）
            anvil_port: Anvil 本地端口
            fork_block: Fork 的区块高度（None 则使用最新块）
            use_wsl: 是否通过 WSL 调用 Anvil（None 则自动检测）
        """
        # 从环境变量读取配置（优先使用 ARBITRUM_RPC_URL，然后是 MAINNET_RPC_URL）
        self.rpc_url = rpc_url or os.getenv("ARBITRUM_RPC_URL") or os.getenv("MAINNET_RPC_URL")
        self.anvil_port = int(os.getenv("ANVIL_PORT", anvil_port))
        self.fork_block = fork_block or (int(os.getenv("FORK_BLOCK_NUMBER")) if os.getenv("FORK_BLOCK_NUMBER") else None)
        
        if not self.rpc_url or "YOUR_API_KEY" in self.rpc_url:
            raise ValueError(
                "请在 .env 文件中配置 ARBITRUM_RPC_URL 或 MAINNET_RPC_URL\n"
                "获取方法：访问 https://www.alchemy.com/ 注册并创建应用"
            )
        
        # 检测操作系统和 WSL
        self.is_windows = platform.system() == "Windows"
        self.is_wsl = "microsoft" in platform.uname().release.lower() if hasattr(platform, "uname") else False
        
        # 决定是否使用 WSL
        if use_wsl is None:
            # 从环境变量读取（如果设置了）
            env_use_wsl = os.getenv("USE_WSL", "").lower()
            if env_use_wsl in ("true", "1", "yes"):
                self.use_wsl = True
            elif env_use_wsl in ("false", "0", "no"):
                self.use_wsl = False
            else:
                # 自动检测：如果在 Windows 且不在 WSL 中，则使用 WSL
                self.use_wsl = self.is_windows and not self.is_wsl
        else:
            self.use_wsl = use_wsl
        
        # WSL 相关配置
        self.wsl_distro = os.getenv("WSL_DISTRO", "Ubuntu")  # 默认使用 Ubuntu
        
        # Anvil 进程
        self.anvil_process: Optional[subprocess.Popen] = None
        
        # Anvil URL：如果在 Windows 上通过 WSL 运行，需要特殊处理
        # WSL2 的端口会自动转发到 Windows，所以仍然可以使用 localhost
        self.anvil_url = f"http://127.0.0.1:{self.anvil_port}"
        
        # Web3 连接（初始化为 None，启动 Anvil 后连接）
        self.w3: Optional[Web3] = None
        
        # 输出目录
        self.output_dir = Path(os.getenv("OUTPUT_DIR", "./outputs"))
        self.trace_dir = Path(os.getenv("TRACE_CACHE_DIR", "./data/traces"))
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        if self.use_wsl:
            logger.info(f"检测到 Windows 系统，将通过 WSL ({self.wsl_distro}) 调用 Anvil")
    
    def test_network_connectivity(self) -> bool:
        """
        测试网络连接（在 WSL 中测试 RPC URL 是否可访问）
        
        Returns:
            True 如果网络连接正常，False 否则
        """
        if not self.use_wsl:
            # 非 WSL 环境，直接返回 True（假设网络正常）
            return True
        
        logger.info("测试 WSL 网络连接...")
        try:
            # 在 WSL 中测试 curl 连接
            test_cmd = ["wsl", "-d", self.wsl_distro, "curl", "-s", "--max-time", "5", self.rpc_url]
            result = subprocess.run(
                test_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10
            )
            
            if result.returncode == 0:
                logger.success("✓ WSL 网络连接正常")
                return True
            else:
                logger.warning(f"⚠ WSL 网络测试失败: {result.stderr[:200]}")
                return False
        except Exception as e:
            logger.warning(f"⚠ 网络测试出错: {e}")
            # 即使测试失败，也继续尝试（可能是 curl 未安装）
            return True  # 返回 True 继续尝试
    
    def get_proposal_creation_block(self, proposal_data: Dict[str, Any]) -> Optional[int]:
        """
        获取提案创建的区块高度
        
        Args:
            proposal_data: 提案数据
            
        Returns:
            提案创建的区块高度，如果无法获取则返回 None
        """
        # 方法1: 从提案数据中直接获取
        if proposal_data.get("block_number"):
            return proposal_data["block_number"]
        
        # 方法2: 从交易哈希获取
        if proposal_data.get("metadata", {}).get("transaction_hash"):
            tx_hash = proposal_data["metadata"]["transaction_hash"]
            logger.info(f"从交易哈希获取区块高度: {tx_hash}")
            try:
                # 创建临时 Web3 连接（timeout 60 秒）
                w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 60}))
                if w3.is_connected():
                    tx = w3.eth.get_transaction(tx_hash)
                    block_number = tx.blockNumber
                    logger.info(f"✓ 找到提案创建区块: {block_number}")
                    return block_number
            except Exception as e:
                logger.warning(f"无法从交易哈希获取区块高度: {e}")
        
        return None
    
    def start_anvil(self, fork_block: Optional[int] = None) -> bool:
        """
        启动 Anvil Fork 进程
        
        Args:
            fork_block: Fork 的区块高度（None 则使用 self.fork_block）
        
        Returns:
            True 如果启动成功，False 否则
        """
        if self.anvil_process is not None:
            logger.warning("Anvil 进程已存在，先停止...")
            self.stop_anvil()
        
        # 使用传入的 fork_block 或实例变量
        fork_block = fork_block if fork_block is not None else self.fork_block
        
        logger.info(f"启动 Anvil Fork (端口: {self.anvil_port})...")
        logger.info(f"Fork URL: {self.rpc_url}")
        
        # 测试网络连接（仅 WSL 环境）
        if self.use_wsl:
            if not self.test_network_connectivity():
                logger.error("WSL 网络连接测试失败，但将继续尝试启动 Anvil")
                logger.info("提示：如果启动失败，请检查：")
                logger.info("1. WSL 是否能访问互联网（在 WSL 中运行: curl https://www.google.com）")
                logger.info("2. RPC URL 是否正确")
                logger.info("3. 是否需要配置代理或 DNS")
        
        # 构建 Anvil 命令
        if self.use_wsl:
            # 通过 WSL 调用 Anvil
            # 注意：WSL2 会自动将端口转发到 Windows，所以可以使用 localhost
            anvil_cmd = [
                "anvil",
                "--fork-url", self.rpc_url,
                "--port", str(self.anvil_port),
                "--host", "0.0.0.0",  # WSL 中需要监听所有接口，以便 Windows 访问
                "--no-cors"
            ]
            
            # 如果指定了区块高度，添加 --fork-block-number
            if fork_block is not None:
                anvil_cmd.extend(["--fork-block-number", str(fork_block)])
                logger.info(f"Fork 区块高度: {fork_block}")
            
            # WSL 命令：wsl -d <distro> <command>
            cmd = ["wsl", "-d", self.wsl_distro] + anvil_cmd
            logger.info(f"通过 WSL 执行: {' '.join(anvil_cmd)}")
        else:
            # 直接调用 Anvil（Linux/Mac 或已在 WSL 中）
            cmd = ["anvil", "--fork-url", self.rpc_url, "--port", str(self.anvil_port)]
            
            # 如果指定了区块高度，添加 --fork-block-number
            if fork_block is not None:
                cmd.extend(["--fork-block-number", str(fork_block)])
                logger.info(f"Fork 区块高度: {fork_block}")
            
            # 添加其他常用参数
            cmd.extend([
                "--host", "127.0.0.1",  # 只监听本地
                "--no-cors",  # 禁用 CORS
            ])
        
        try:
            # 启动 Anvil 进程（后台运行）
            # 注意：在 Windows 上通过 WSL 调用时，输出是 UTF-8 编码
            # 需要明确指定 encoding，避免使用系统默认编码（GBK）
            encoding = 'utf-8'
            errors = 'replace'  # 遇到无法解码的字符时替换为占位符
            
            self.anvil_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=encoding,
                errors=errors,
                bufsize=1
            )
            
            # 等待 Anvil 启动（最多等待 30 秒）
            logger.info("等待 Anvil 启动...")
            max_wait = 30
            wait_interval = 0.5
            waited = 0
            
            while waited < max_wait:
                try:
                    # 尝试连接（timeout 60 秒）
                    test_w3 = Web3(Web3.HTTPProvider(self.anvil_url, request_kwargs={"timeout": 60}))
                    if test_w3.is_connected():
                        block_number = test_w3.eth.block_number
                        logger.success(f"✓ Anvil 已启动，当前区块: {block_number:,}")
                        self.w3 = test_w3
                        return True
                except Exception:
                    pass
                
                # 检查进程是否还在运行
                if self.anvil_process.poll() is not None:
                    # 进程已退出，读取错误信息
                    try:
                        stdout, stderr = self.anvil_process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        # 如果读取超时，强制终止并获取部分输出
                        self.anvil_process.kill()
                        stdout, stderr = self.anvil_process.communicate()
                    
                    logger.error(f"Anvil 进程异常退出")
                    if stdout:
                        logger.error(f"STDOUT: {stdout[:500]}")  # 只显示前500字符
                    if stderr:
                        # 提取关键错误信息
                        stderr_clean = stderr[:1000]  # 显示前1000字符
                        logger.error(f"STDERR: {stderr_clean}")
                        
                        # 检查是否是网络连接错误
                        if "failed to fetch" in stderr.lower() or "connect" in stderr.lower():
                            logger.error("=" * 60)
                            logger.error("网络连接错误 - 故障排除建议：")
                            logger.error("=" * 60)
                            if self.use_wsl:
                                logger.error("1. 在 WSL 中测试网络连接：")
                                logger.error(f"   wsl -d {self.wsl_distro} curl -I {self.rpc_url}")
                                logger.error("2. 检查 WSL 网络配置：")
                                logger.error(f"   wsl -d {self.wsl_distro} ping 8.8.8.8")
                                logger.error("3. 如果使用代理，确保 WSL 中配置了代理")
                                logger.error("4. 尝试在 WSL 中手动运行 Anvil 测试：")
                                logger.error(f"   wsl -d {self.wsl_distro} anvil --fork-url {self.rpc_url} --port {self.anvil_port}")
                            else:
                                logger.error("1. 检查网络连接和防火墙设置")
                                logger.error("2. 验证 RPC URL 是否正确")
                                logger.error("3. 尝试使用其他 RPC 提供商")
                            logger.error("=" * 60)
                    
                    self.anvil_process = None
                    return False
                
                time.sleep(wait_interval)
                waited += wait_interval
            
            logger.error(f"Anvil 启动超时（{max_wait} 秒）")
            self.stop_anvil()
            return False
            
        except FileNotFoundError:
            if self.use_wsl:
                logger.error("未找到 wsl 命令或 Anvil 未在 WSL 中安装")
                logger.error("请确保：")
                logger.error("1. WSL 已正确安装和配置")
                logger.error("2. 在 WSL 中安装 Foundry: curl -L https://foundry.paradigm.xyz | bash")
                logger.error("3. 如果使用非默认发行版，请在 .env 中设置 WSL_DISTRO 环境变量")
            else:
                logger.error("未找到 anvil 命令，请确保 Foundry 已安装并在 PATH 中")
                logger.error("安装方法: curl -L https://foundry.paradigm.xyz | bash")
            return False
        except Exception as e:
            logger.error(f"启动 Anvil 失败: {e}")
            self.stop_anvil()
            return False
    
    def stop_anvil(self):
        """停止 Anvil 进程"""
        if self.anvil_process is not None:
            logger.info("停止 Anvil 进程...")
            try:
                # 发送 SIGTERM 信号
                self.anvil_process.terminate()
                
                # 等待进程结束（最多 5 秒）
                try:
                    self.anvil_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 如果 5 秒内没有结束，强制杀死
                    logger.warning("Anvil 进程未正常退出，强制终止...")
                    self.anvil_process.kill()
                    self.anvil_process.wait()
                
                logger.success("✓ Anvil 已停止")
            except Exception as e:
                logger.warning(f"停止 Anvil 时出错: {e}")
            finally:
                self.anvil_process = None
                self.w3 = None
    
    def impersonate_account(self, address: str, balance_eth: float = 100.0) -> bool:
        """
        使用 Anvil 的账户伪装功能
        
        Args:
            address: 要伪装的地址
            balance_eth: 设置的余额（ETH）
            
        Returns:
            True 如果成功，False 否则
        """
        if self.w3 is None:
            raise RuntimeError("Anvil 未启动，请先调用 start_anvil()")
        
        address = Web3.to_checksum_address(address)
        balance_wei = int(balance_eth * 1e18)
        
        try:
            # 1. 设置余额
            logger.info(f"设置账户余额: {address} -> {balance_eth} ETH")
            self.w3.manager.request_blocking(
                "anvil_setBalance",
                [address, hex(balance_wei)]
            )
            
            # 2. 伪装账户
            logger.info(f"伪装账户: {address}")
            self.w3.manager.request_blocking(
                "anvil_impersonateAccount",
                [address]
            )
            
            # 验证余额
            actual_balance = self.w3.eth.get_balance(address)
            logger.success(f"✓ 账户已伪装，余额: {actual_balance / 1e18:.6f} ETH")
            return True
            
        except Exception as e:
            logger.error(f"伪装账户失败: {e}")
            return False
    
    def execute_proposal(
        self,
        proposal_data: Dict[str, Any],
        from_address: Optional[str] = None,
        use_proposer: bool = True
    ) -> Optional[str]:
        """
        在 Anvil 中执行提案（使用账户伪装）
        
        Args:
            proposal_data: 提案数据（从 collected_proposal.json 读取）
            from_address: 发送交易的地址（None 则使用提案的 proposer）
            use_proposer: 如果 from_address 为 None，是否使用提案的 proposer 地址
            
        Returns:
            交易哈希，如果失败则返回 None
        """
        if self.w3 is None:
            raise RuntimeError("Anvil 未启动，请先调用 start_anvil()")
        
        logger.info(f"{'='*60}")
        logger.info(f"执行提案: {proposal_data.get('title', 'Unknown')}")
        logger.info(f"{'='*60}")
        
        # 确定执行地址（优先使用 proposer）
        if from_address is None:
            if use_proposer and proposal_data.get("proposer"):
                from_address = Web3.to_checksum_address(proposal_data["proposer"])
                logger.info(f"使用提案 proposer 地址: {from_address}")
            else:
                # 使用 Anvil 默认账户（索引 0）
                accounts = self.w3.eth.accounts
                if not accounts:
                    raise RuntimeError("Anvil 中没有可用账户")
                from_address = accounts[0]
                logger.info(f"使用默认账户: {from_address}")
        else:
            from_address = Web3.to_checksum_address(from_address)
        
        # 使用 Anvil 账户伪装功能
        if not self.impersonate_account(from_address, balance_eth=100.0):
            logger.error("账户伪装失败，无法继续执行")
            return None
        
        # 提取提案参数
        targets = proposal_data.get("targets", [])
        values = proposal_data.get("values", [])
        calldatas = proposal_data.get("calldatas", [])
        
        if not targets or not calldatas:
            logger.error("提案数据无效：缺少 targets 或 calldatas")
            return None
        
        if len(targets) != len(calldatas):
            logger.error(f"提案数据不匹配：targets({len(targets)}) != calldatas({len(calldatas)})")
            return None
        
        # 获取当前 nonce
        nonce = self.w3.eth.get_transaction_count(from_address)
        
        # 构建交易（执行第一个 target/calldata，通常提案只有一个）
        # 注意：如果是多目标提案，这里只执行第一个，可以根据需要扩展
        target = Web3.to_checksum_address(targets[0])
        value = values[0] if len(values) > 0 else 0
        
        # 处理 calldata（可能是字符串或已经是十六进制格式）
        calldata_str = calldatas[0]
        if isinstance(calldata_str, str):
            # 移除 0x 前缀（如果有）
            calldata_str = calldata_str.replace("0x", "")
            calldata = bytes.fromhex(calldata_str)
        else:
            calldata = calldata_str
        
        logger.info(f"目标合约: {target}")
        logger.info(f"ETH 转账: {value} wei")
        logger.info(f"Calldata 长度: {len(calldata)} bytes")
        
        # 构建交易
        transaction = {
            "from": from_address,
            "to": target,
            "value": value,
            "data": calldata,
            "gas": 10_000_000,  # 设置较大的 gas limit
            "gasPrice": self.w3.eth.gas_price,
            "nonce": nonce,
        }
        
        try:
            # 先使用 eth_call 模拟执行，获取 revert reason
            logger.info("模拟执行交易（eth_call）...")
            try:
                result = self.w3.eth.call(transaction)
                logger.success("✓ 模拟执行成功，可以继续")
            except Exception as call_error:
                error_msg = str(call_error)
                logger.warning(f"⚠ 模拟执行失败: {error_msg}")
                
                # 尝试提取 revert reason
                if "revert" in error_msg.lower() or "execution reverted" in error_msg.lower():
                    logger.error("=" * 60)
                    logger.error("交易模拟执行失败（revert）- 可能的原因：")
                    logger.error("=" * 60)
                    logger.error("1. 提案可能已经执行过，无法重复执行")
                    logger.error("2. 提案需要满足特定条件（如投票通过、时间窗口等）")
                    logger.error("3. 执行账户缺少必要的权限")
                    logger.error("4. Fork 的区块高度不正确，导致状态不一致")
                    logger.error("=" * 60)
                    logger.error("建议：")
                    logger.error("- 检查提案是否已经执行过")
                    logger.error("- 尝试使用提案创建时的 proposer 地址作为 from_address")
                    logger.error("- 检查 Fork 的区块高度是否正确")
                    logger.error("=" * 60)
                
                # 即使模拟失败，也尝试实际执行（某些情况下可能不同）
                logger.info("继续尝试实际执行交易...")
            
            # 发送交易（使用伪装的账户，不需要签名）
            logger.info("发送交易（使用账户伪装）...")
            # 注意：使用账户伪装时，from 地址不需要有私钥，Anvil 会自动处理
            tx_hash = self.w3.eth.send_transaction(transaction)
            logger.success(f"✓ 交易已发送: {tx_hash.hex()}")
            
            # 等待交易确认
            logger.info("等待交易确认...")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt.status == 1:
                logger.success(f"✓ 交易成功，区块: {receipt.blockNumber}")
                return tx_hash.hex()
            else:
                logger.error(f"✗ 交易失败（revert）")
                
                # 尝试获取 revert reason
                try:
                    # 使用 debug_traceCall 获取详细的 revert 信息
                    trace_result = self.w3.manager.request_blocking(
                        "debug_traceCall",
                        [
                            transaction,
                            "latest",
                            {"tracer": "callTracer"}
                        ]
                    )
                    if trace_result and trace_result.get("error"):
                        logger.error(f"Revert 原因: {trace_result.get('error')}")
                except Exception:
                    pass
                
                logger.error("=" * 60)
                logger.error("交易执行失败 - 可能的原因：")
                logger.error("=" * 60)
                logger.error("1. 提案可能已经执行过，无法重复执行")
                logger.error("2. 提案需要满足特定条件（如投票通过、时间窗口等）")
                logger.error("3. 执行账户缺少必要的权限")
                logger.error("4. Fork 的区块高度不正确，导致状态不一致")
                logger.error("=" * 60)
                logger.error("建议：")
                logger.error("- 检查提案是否已经执行过")
                logger.error("- 尝试使用提案创建时的 proposer 地址作为 from_address")
                logger.error("- 检查 Fork 的区块高度是否正确")
                logger.error("- 如果提案需要投票，确保在 Fork 的区块高度上投票已通过")
                logger.error("=" * 60)
                
                return None
                
        except Exception as e:
            logger.error(f"执行交易失败: {e}")
            logger.exception("详细错误:")
            return None
    
    def get_trace(self, tx_hash: str) -> Optional[Dict[str, Any]]:
        """
        获取交易的执行轨迹（Trace）
        
        Args:
            tx_hash: 交易哈希
            
        Returns:
            Trace 数据字典，如果失败则返回 None
        """
        if self.w3 is None:
            raise RuntimeError("Anvil 未启动，请先调用 start_anvil()")
        
        logger.info(f"获取交易 Trace: {tx_hash}")
        
        try:
            # 使用 debug_traceTransaction 获取 trace
            # Anvil 支持 Geth 格式的 trace
            # 尝试使用 callTracer（更详细）
            try:
                trace = self.w3.manager.request_blocking(
                    "debug_traceTransaction",
                    [tx_hash, {"tracer": "callTracer", "tracerConfig": {"withLog": True}}]
                )
            except Exception as e:
                logger.warning(f"callTracer 失败，尝试默认 tracer: {e}")
                # 如果 callTracer 失败，尝试默认 tracer
                trace = self.w3.manager.request_blocking(
                    "debug_traceTransaction",
                    [tx_hash, {}]
                )
            
            logger.success("✓ Trace 获取成功")
            return trace
            
        except Exception as e:
            logger.error(f"获取 Trace 失败: {e}")
            logger.exception("详细错误:")
            return None
    
    def get_trace_with_js_tracer(self, tx_hash: str) -> Optional[List[Dict[str, Any]]]:
        """
        使用 JavaScript Tracer 获取精简的 Trace（漏洞补丁3：内存保护）
        只捕获 CALL, STATICCALL, DELEGATECALL 指令
        
        Args:
            tx_hash: 交易哈希
            
        Returns:
            精简后的 Trace 列表，如果失败则返回 None
        """
        if self.w3 is None:
            raise RuntimeError("Anvil 未启动，请先调用 start_anvil()")
        
        logger.info(f"使用 JavaScript Tracer 获取交易 Trace: {tx_hash}")
        
        # JavaScript Tracer 代码：只捕获 CALL, STATICCALL, DELEGATECALL
        # 注意：Anvil 使用 Geth 的 JavaScript Tracer API
        js_tracer_code = """
        {
            result: [],
            step: function(log, db) {
                var op = log.op.toString();
                // 只捕获 CALL, STATICCALL, DELEGATECALL
                if (op === 'CALL' || op === 'STATICCALL' || op === 'DELEGATECALL') {
                    try {
                        var stack = log.stack;
                        var memory = log.memory;
                        var contract = log.contract;
                        
                        // 获取调用信息
                        var fromAddr = toHex(contract.getAddress());
                        var toAddr = '';
                        var value = '0';
                        var inputData = '0x';
                        var gas = '0';
                        
                        if (op === 'CALL') {
                            // CALL: stack[0]=gas, stack[1]=to, stack[2]=value, stack[3]=inOffset, stack[4]=inSize, stack[5]=outOffset, stack[6]=outSize
                            toAddr = toHex(toAddress(stack.peek(1).toString(16)));
                            value = stack.peek(2).toString();
                            var inOffset = parseInt(stack.peek(3).toString());
                            var inSize = parseInt(stack.peek(4).toString());
                            if (inSize > 0 && memory.length > inOffset + inSize) {
                                inputData = toHex(memory.slice(inOffset, inOffset + inSize));
                            }
                            gas = stack.peek(0).toString();
                        } else if (op === 'STATICCALL') {
                            // STATICCALL: stack[0]=gas, stack[1]=to, stack[2]=inOffset, stack[3]=inSize, stack[4]=outOffset, stack[5]=outSize
                            toAddr = toHex(toAddress(stack.peek(1).toString(16)));
                            var inOffset = parseInt(stack.peek(2).toString());
                            var inSize = parseInt(stack.peek(3).toString());
                            if (inSize > 0 && memory.length > inOffset + inSize) {
                                inputData = toHex(memory.slice(inOffset, inOffset + inSize));
                            }
                            gas = stack.peek(0).toString();
                        } else if (op === 'DELEGATECALL') {
                            // DELEGATECALL: stack[0]=gas, stack[1]=to, stack[2]=inOffset, stack[3]=inSize, stack[4]=outOffset, stack[5]=outSize
                            toAddr = toHex(toAddress(stack.peek(1).toString(16)));
                            var inOffset = parseInt(stack.peek(2).toString());
                            var inSize = parseInt(stack.peek(3).toString());
                            if (inSize > 0 && memory.length > inOffset + inSize) {
                                inputData = toHex(memory.slice(inOffset, inOffset + inSize));
                            }
                            gas = stack.peek(0).toString();
                        }
                        
                        var callInfo = {
                            type: op,
                            from: fromAddr,
                            to: toAddr,
                            value: value,
                            input: inputData,
                            gas: gas,
                            depth: log.getDepth()
                        };
                        
                        // 提取函数选择器（前4字节）
                        if (callInfo.input && callInfo.input.length >= 10) {
                            callInfo.function_selector = callInfo.input.substring(0, 10);
                        } else {
                            callInfo.function_selector = '0x';
                        }
                        
                        this.result.push(callInfo);
                    } catch (e) {
                        // 忽略解析错误，继续处理
                    }
                }
            },
            fault: function(log, db) {},
            result: function(ctx, db) {
                return this.result;
            }
        }
        """
        
        try:
            trace_result = self.w3.manager.request_blocking(
                "debug_traceTransaction",
                [tx_hash, {"tracer": js_tracer_code}]
            )
            
            logger.success(f"✓ JavaScript Tracer 获取成功，捕获 {len(trace_result)} 个调用")
            return trace_result
            
        except Exception as e:
            logger.error(f"JavaScript Tracer 获取失败: {e}")
            logger.exception("详细错误:")
            return None
    
    def extract_calls_and_transfers(self, trace: Dict[str, Any]) -> Dict[str, Any]:
        """
        从 Trace 中提取跨合约调用和资金转移
        
        Args:
            trace: Trace 数据字典
            
        Returns:
            结构化的调用和转账信息
        """
        calls = []
        transfers = []
        
        def traverse_trace(node: Dict[str, Any], depth: int = 0):
            """递归遍历 trace 树"""
            if depth > 50:  # 防止无限递归
                return
            
            # 提取调用信息
            call_type = node.get("type", "")
            from_addr = node.get("from", "")
            to_addr = node.get("to", "")
            
            # 处理 value（可能是十六进制字符串或数字）
            value_raw = node.get("value", "0x0")
            if isinstance(value_raw, str):
                value = int(value_raw, 16) if value_raw.startswith("0x") else int(value_raw)
            else:
                value = value_raw
            
            input_data = node.get("input", "0x")
            output_data = node.get("output", "0x")
            
            # 处理 gas_used（可能是十六进制字符串或数字）
            gas_used_raw = node.get("gasUsed", "0x0")
            if isinstance(gas_used_raw, str):
                gas_used = int(gas_used_raw, 16) if gas_used_raw.startswith("0x") else int(gas_used_raw)
            else:
                gas_used = gas_used_raw
            
            error = node.get("error", None)
            
            # 记录跨合约调用（CALL, DELEGATECALL, STATICCALL, CALLCODE）
            if call_type in ["CALL", "DELEGATECALL", "STATICCALL", "CALLCODE"]:
                call_info = {
                    "type": call_type,
                    "from": from_addr,
                    "to": to_addr,
                    "value": value,
                    "input": input_data,
                    "output": output_data,
                    "gas_used": gas_used,
                    "depth": depth,
                    "error": error
                }
                calls.append(call_info)
                
                # 如果有 value 转移，记录为转账
                if value > 0:
                    transfer_info = {
                        "from": from_addr,
                        "to": to_addr,
                        "value": value,
                        "value_eth": value / 1e18,  # 转换为 ETH
                        "call_type": call_type,
                        "depth": depth
                    }
                    transfers.append(transfer_info)
            
            # 递归处理子调用
            if "calls" in node:
                for child in node["calls"]:
                    traverse_trace(child, depth + 1)
        
        # 开始遍历
        traverse_trace(trace)
        
        # 构建摘要
        summary = {
            "total_calls": len(calls),
            "total_transfers": len(transfers),
            "total_value_transferred": sum(t["value"] for t in transfers),
            "total_value_transferred_eth": sum(t["value_eth"] for t in transfers),
            "calls": calls,
            "transfers": transfers
        }
        
        logger.info(f"提取完成: {len(calls)} 个调用, {len(transfers)} 个转账")
        
        return summary
    
    def replay_transaction(
        self,
        target_tx_hash: str,
        output_file: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        核心函数：从交易哈希开始，在本地 Anvil 环境中完美重放交易并捕获执行轨迹
        
        实现三个关键漏洞补丁：
        1. 时空一致性：自动计算 fork-block-number = 原始区块 - 1
        2. 时间戳同步：从原始区块获取 timestamp，调用 evm_setNextBlockTimestamp
        3. Trace 捕获：使用稳定的 callTracer 获取执行轨迹，并解析函数签名
        
        Args:
            target_tx_hash: 目标交易哈希（DAO 提案执行交易）
            output_file: 输出文件路径（None 则使用默认路径 data/traces/trace_report.json）
            
        Returns:
            包含 Trace 轨迹和摘要的字典，如果失败则返回 None
        """
        logger.info(f"{'='*60}")
        logger.info(f"开始重放交易: {target_tx_hash}")
        logger.info(f"{'='*60}")
        
        # 创建输出目录
        traces_dir = Path("data/traces")
        traces_dir.mkdir(parents=True, exist_ok=True)
        
        # 连接到主网 RPC 获取原始交易信息（timeout 60 秒）
        logger.info("连接到主网 RPC 获取原始交易信息...")
        mainnet_w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 60}))
        if not mainnet_w3.is_connected():
            logger.error("无法连接到主网 RPC")
            return None
        
        try:
            # 1. 获取原始交易元数据
            logger.info(f"获取交易信息: {target_tx_hash}")
            original_tx = mainnet_w3.eth.get_transaction(target_tx_hash)
            original_receipt = mainnet_w3.eth.get_transaction_receipt(target_tx_hash)
            
            original_block_number = original_tx.blockNumber
            original_from = original_tx['from']
            original_to = original_tx.to
            original_value = original_tx.value
            original_data = original_tx.input
            original_gas = original_tx.gas
            
            logger.info(f"原始交易信息:")
            logger.info(f"  区块高度: {original_block_number}")
            logger.info(f"  发送方: {original_from}")
            logger.info(f"  接收方: {original_to}")
            logger.info(f"  金额: {original_value / 1e18:.6f} ETH")
            logger.info(f"  状态: {'成功' if original_receipt.status == 1 else '失败'}")
            
            # 漏洞补丁1：自动计算 fork-block-number = 原始区块 - 1
            fork_block_number = original_block_number - 1
            logger.info(f"漏洞补丁1: Fork 到区块 {fork_block_number} (原始区块 {original_block_number} - 1)")
            
            # 获取原始区块的 timestamp（漏洞补丁2）
            original_block = mainnet_w3.eth.get_block(original_block_number)
            original_timestamp = original_block['timestamp']
            logger.info(f"漏洞补丁2: 原始区块时间戳: {original_timestamp}")
            
            # 2. 启动 Anvil Fork 环境
            logger.info("启动 Anvil Fork 环境...")
            if not self.start_anvil(fork_block=fork_block_number):
                logger.error("无法启动 Anvil，重放终止")
                return None
            
            try:
                # 漏洞补丁2：设置时间戳（确保时间敏感型提案不会过期）
                logger.info(f"设置区块时间戳: {original_timestamp}")
                try:
                    self.w3.manager.request_blocking(
                        "evm_setNextBlockTimestamp",
                        [hex(original_timestamp)]
                    )
                    logger.success("✓ 时间戳已同步")
                except Exception as e:
                    logger.warning(f"设置时间戳失败（可能 Anvil 版本不支持）: {e}")
                
                # 3. 上帝模式配置：伪装账户并注入余额
                logger.info("配置上帝模式...")
                if not self.impersonate_account(original_from, balance_eth=100.0):
                    logger.error("账户伪装失败，无法继续")
                    return None
                
                # 4. 构建重放交易（使用最大 gas limit）
                logger.info("构建重放交易...")
                nonce = self.w3.eth.get_transaction_count(original_from)
                
                replay_tx = {
                    "from": original_from,
                    "to": original_to,
                    "value": original_value,
                    "data": original_data,
                    "gas": 30_000_000,  # 最大 gas limit，避免 Out of Gas
                    "gasPrice": self.w3.eth.gas_price,
                    "nonce": nonce,
                }
                
                logger.info(f"Gas Limit: {replay_tx['gas']:,}")
                
                # 5. 先使用 eth_call 模拟执行，获取 revert reason
                logger.info("模拟执行交易（eth_call）...")
                try:
                    result = self.w3.eth.call(replay_tx)
                    logger.success("✓ 模拟执行成功")
                except Exception as call_error:
                    error_msg = str(call_error)
                    logger.warning(f"⚠ 模拟执行失败: {error_msg}")
                    
                    # 尝试提取 revert reason
                    if "revert" in error_msg.lower() or "execution reverted" in error_msg.lower():
                        logger.error("=" * 60)
                        logger.error("交易模拟执行失败（revert）")
                        logger.error(f"错误信息: {error_msg}")
                        logger.error("=" * 60)
                    
                    # 即使模拟失败，也尝试实际执行
                    logger.info("继续尝试实际执行交易...")
                
                # 6. 发送交易（使用伪装的账户，不需要签名）
                logger.info("发送重放交易...")
                try:
                    tx_hash = self.w3.eth.send_transaction(replay_tx)
                    logger.success(f"✓ 交易已发送: {tx_hash.hex()}")
                    
                    # 等待交易确认
                    logger.info("等待交易确认...")
                    receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    
                    if receipt.status == 1:
                        logger.success(f"✓ 交易成功，区块: {receipt.blockNumber}")
                    else:
                        logger.error(f"✗ 交易失败（revert）")
                        
                        # 尝试获取 revert reason
                        try:
                            trace_result = self.w3.manager.request_blocking(
                                "debug_traceCall",
                                [
                                    replay_tx,
                                    "latest",
                                    {"tracer": "callTracer"}
                                ]
                            )
                            if trace_result and trace_result.get("error"):
                                logger.error(f"Revert 原因: {trace_result.get('error')}")
                        except Exception:
                            pass
                        
                        logger.error("=" * 60)
                        logger.error("交易执行失败 - 可能的原因：")
                        logger.error("1. 提案可能已经执行过，无法重复执行")
                        logger.error("2. 提案需要满足特定条件（如投票通过、时间窗口等）")
                        logger.error("3. Fork 的区块高度不正确，导致状态不一致")
                        logger.error("4. 时间戳设置可能不正确")
                        logger.error("=" * 60)
                        
                        # 即使失败，也尝试获取 Trace
                        logger.info("继续尝试获取 Trace...")
                    
                    # 7. 使用 callTracer 获取 Trace（稳定性优化：移除不稳定的 JavaScript Tracer）
                    logger.info("使用 callTracer 获取执行轨迹...")
                    try:
                        full_trace = self.w3.manager.request_blocking(
                            "debug_traceTransaction",
                            [tx_hash.hex(), {"tracer": "callTracer"}]
                        )
                        # 从 callTracer 结果中提取调用
                        trace_calls = self._extract_calls_from_call_tracer(full_trace)
                        logger.success(f"✓ callTracer 获取成功，捕获 {len(trace_calls)} 个调用")
                    except Exception as e:
                        logger.error(f"获取 Trace 失败: {e}")
                        logger.exception("详细错误:")
                        trace_calls = []
                    
                    # 8. 处理 Trace 数据
                    if trace_calls:
                        # 转换数据格式
                        processed_calls = []
                        max_depth = 0
                        total_calls = len(trace_calls)
                        
                        for call in trace_calls:
                            # 处理 value（可能是字符串或数字）
                            value_str = call.get("value", "0")
                            if isinstance(value_str, str):
                                value = int(value_str) if value_str.isdigit() else int(value_str, 16) if value_str.startswith("0x") else 0
                            else:
                                value = value_str
                            
                            # 处理 gas（可能是字符串或数字）
                            gas_str = call.get("gas", "0")
                            if isinstance(gas_str, str):
                                gas = int(gas_str) if gas_str.isdigit() else int(gas_str, 16) if gas_str.startswith("0x") else 0
                            else:
                                gas = gas_str
                            
                            depth = call.get("depth", 0)
                            if depth > max_depth:
                                max_depth = depth
                            
                            # 提取函数选择器（前4字节）并解析函数签名
                            input_data = call.get("input", "0x")
                            function_selector = input_data[:10] if len(input_data) >= 10 else "0x"
                            function_signature = resolve_function_signature(function_selector)
                            
                            processed_call = {
                                "type": call.get("type", "UNKNOWN"),
                                "from": call.get("from", ""),
                                "to": call.get("to", ""),
                                "value": value,
                                "value_eth": value / 1e18,
                                "input": input_data,
                                "function_selector": function_selector,
                                "function_signature": function_signature,  # 可读的函数签名
                                "gas": gas,
                                "depth": depth  # 确保保留 depth 字段
                            }
                            processed_calls.append(processed_call)
                        
                        # 9. 构建结果
                        result = {
                            "original_transaction": {
                                "hash": target_tx_hash,
                                "block_number": original_block_number,
                                "from": original_from,
                                "to": original_to,
                                "value": str(original_value),
                                "status": "success" if original_receipt.status == 1 else "failed"
                            },
                            "replay_transaction": {
                                "hash": tx_hash.hex(),
                                "status": "success" if receipt.status == 1 else "failed"
                            },
                            "fork_config": {
                                "fork_block_number": fork_block_number,
                                "original_block_number": original_block_number,
                                "timestamp": original_timestamp
                            },
                            "trace_summary": {
                                "total_calls": total_calls,
                                "max_depth": max_depth,
                                "calls": processed_calls
                            },
                            "trace_calls": trace_calls  # 原始 Trace 数据
                        }
                        
                        # 10. 保存结果
                        if output_file is None:
                            output_file = traces_dir / "trace_report.json"
                        else:
                            output_file = Path(output_file)
                        
                        output_file.parent.mkdir(parents=True, exist_ok=True)
                        
                        with open(output_file, 'w', encoding='utf-8') as f:
                            # 转换 AttributeDict 为普通字典
                            serializable_result = convert_to_serializable(result)
                            json.dump(serializable_result, f, indent=2, ensure_ascii=False)  

                        logger.success(f"✓ Trace 报告已保存: {output_file}")
                        
                        # 11. 输出文本摘要
                        logger.info(f"{'='*60}")
                        logger.info(f"交易重放摘要:")
                        logger.info(f"{'='*60}")
                        logger.info(f"原始交易: {target_tx_hash}")
                        logger.info(f"重放交易: {tx_hash.hex()}")
                        logger.info(f"执行状态: {'成功' if receipt.status == 1 else '失败'}")
                        logger.info(f"Fork 区块: {fork_block_number} (原始区块 {original_block_number} - 1)")
                        logger.info(f"时间戳: {original_timestamp}")
                        logger.info(f"总调用数: {total_calls}")
                        logger.info(f"最大深度: {max_depth}")
                        logger.info(f"{'='*60}")
                        
                        return result
                    else:
                        logger.error("无法获取 Trace 数据")
                        return None
                        
                except Exception as e:
                    logger.error(f"执行交易失败: {e}")
                    logger.exception("详细错误:")
                    return None
                    
            finally:
                # 确保停止 Anvil（资源释放）
                logger.info("清理 Anvil 进程...")
                self.stop_anvil()
                
        except TransactionNotFound:
            logger.error(f"交易不存在: {target_tx_hash}")
            return None
        except Exception as e:
            logger.error(f"重放交易失败: {e}")
            logger.exception("详细错误:")
            self.stop_anvil()
            return None
    
    def _extract_calls_from_call_tracer(self, trace: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从 callTracer 结果中提取调用信息
        
        Args:
            trace: callTracer 返回的完整 trace
            
        Returns:
            调用列表（包含 depth 字段和函数签名）
        """
        calls = []
        
        def traverse(node: Dict[str, Any], depth: int = 0):
            call_type = node.get("type", "")
            if call_type in ["CALL", "STATICCALL", "DELEGATECALL"]:
                value_raw = node.get("value", "0x0")
                if isinstance(value_raw, str):
                    value = int(value_raw, 16) if value_raw.startswith("0x") else int(value_raw)
                else:
                    value = value_raw
                
                input_data = node.get("input", "0x")
                function_selector = input_data[:10] if len(input_data) >= 10 else "0x"
                function_signature = resolve_function_signature(function_selector)
                
                # 处理 gas（可能是十六进制字符串或数字）
                gas_raw = node.get("gas", "0")
                if isinstance(gas_raw, str):
                    gas = int(gas_raw, 16) if gas_raw.startswith("0x") else int(gas_raw) if gas_raw.isdigit() else 0
                else:
                    gas = gas_raw
                
                calls.append({
                    "type": call_type,
                    "from": node.get("from", ""),
                    "to": node.get("to", ""),
                    "value": str(value),
                    "input": input_data,
                    "function_selector": function_selector,
                    "function_signature": function_signature,  # 可读的函数签名
                    "gas": str(gas),
                    "depth": depth  # 确保保留 depth 字段
                })
            
            if "calls" in node:
                for child in node["calls"]:
                    traverse(child, depth + 1)
        
        traverse(trace)
        return calls
    
    def get_trace_from_chain(
        self,
        tx_hash: str,
        rpc_url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        直接从链上获取已执行交易的 Trace（不需要 Anvil）
        
        Args:
            tx_hash: 交易哈希
            rpc_url: RPC URL（None 则使用初始化时的 RPC URL）
            
        Returns:
            Trace 数据字典，如果失败则返回 None
        """
        rpc = rpc_url or self.rpc_url
        if not rpc:
            raise ValueError("需要提供 RPC URL")
        
        logger.info(f"从链上获取交易 Trace: {tx_hash}")
        logger.info(f"使用 RPC: {rpc[:50]}...")
        
        try:
            # 创建临时 Web3 连接（timeout 60 秒）
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 60}))
            if not w3.is_connected():
                logger.error("无法连接到 RPC 节点")
                return None
            
            # 检查交易是否存在
            try:
                tx = w3.eth.get_transaction(tx_hash)
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                
                if receipt.status == 0:
                    logger.warning("交易已执行但失败（revert），仍将尝试获取 Trace")
                
                logger.info(f"交易区块: {receipt.blockNumber}, 状态: {'成功' if receipt.status == 1 else '失败'}")
            except Exception as e:
                logger.error(f"交易不存在或无法访问: {e}")
                return None
            
            # 获取 Trace
            try:
                trace = w3.manager.request_blocking(
                    "debug_traceTransaction",
                    [tx_hash, {"tracer": "callTracer", "tracerConfig": {"withLog": True}}]
                )
                logger.success("✓ Trace 获取成功")
                return trace
            except Exception as e:
                logger.warning(f"callTracer 失败，尝试默认 tracer: {e}")
                try:
                    trace = w3.manager.request_blocking(
                        "debug_traceTransaction",
                        [tx_hash, {}]
                    )
                    logger.success("✓ Trace 获取成功（使用默认 tracer）")
                    return trace
                except Exception as e2:
                    logger.error(f"获取 Trace 失败: {e2}")
                    logger.error("提示：某些 RPC 提供商可能不支持 debug_traceTransaction")
                    logger.error("建议：使用支持 debug API 的 RPC（如本地节点或 Alchemy/Infura 的付费计划）")
                    return None
                    
        except Exception as e:
            logger.error(f"从链上获取 Trace 失败: {e}")
            logger.exception("详细错误:")
            return None
    
    def simulate_proposal(
        self,
        proposal_file: str,
        output_file: Optional[str] = None,
        use_existing_tx: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        完整的模拟流程：启动 Anvil -> 执行提案 -> 获取 Trace -> 提取信息
        
        如果提案已执行且提供了交易哈希，可以直接从链上获取 Trace
        
        Args:
            proposal_file: 提案 JSON 文件路径
            output_file: 输出文件路径（None 则自动生成）
            use_existing_tx: 如果提案有原始交易哈希，是否优先使用（True 则直接从链上获取）
            
        Returns:
            Trace 摘要字典，如果失败则返回 None
        """
        try:
            # 1. 读取提案数据
            logger.info(f"读取提案文件: {proposal_file}")
            with open(proposal_file, 'r', encoding='utf-8') as f:
                proposal_data = json.load(f)
            
            # 检查是否有原始交易哈希（提案已执行）
            original_tx_hash = None
            if use_existing_tx and proposal_data.get("metadata", {}).get("transaction_hash"):
                original_tx_hash = proposal_data["metadata"]["transaction_hash"]
                logger.info(f"检测到原始交易哈希: {original_tx_hash}")
                logger.info("尝试直接从链上获取 Trace（提案可能已执行）...")
            
            # 如果提供了原始交易哈希，尝试直接从链上获取
            if original_tx_hash and use_existing_tx:
                try:
                    trace = self.get_trace_from_chain(original_tx_hash)
                    if trace:
                        logger.success("✓ 成功从链上获取 Trace（提案已执行）")
                        
                        # 提取调用和转账信息
                        summary = self.extract_calls_and_transfers(trace)
                        
                        # 构建完整结果
                        result = {
                            "proposal_id": proposal_data.get("id"),
                            "proposal_title": proposal_data.get("title"),
                            "transaction_hash": original_tx_hash,
                            "source": "chain",  # 标记来源为链上
                            "trace": trace,
                            "summary": summary
                        }
                        
                        # 保存结果
                        if output_file is None:
                            proposal_id = str(proposal_data.get("id", "unknown"))
                            output_file = self.trace_dir / f"trace_summary_{proposal_id}.json"
                        
                        output_path = Path(output_file)
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        
                        with open(output_path, 'w', encoding='utf-8') as f:
                            # 转换 AttributeDict 为普通字典
                            serializable_result = convert_to_serializable(result)
                            json.dump(serializable_result, f, indent=2, ensure_ascii=False)
                        
                        logger.success(f"✓ Trace 摘要已保存: {output_path}")
                        
                        # 打印摘要
                        logger.info(f"{'='*60}")
                        logger.info(f"Trace 摘要（来自链上）:")
                        logger.info(f"  总调用数: {summary['total_calls']}")
                        logger.info(f"  总转账数: {summary['total_transfers']}")
                        logger.info(f"  总转账金额: {summary['total_value_transferred_eth']:.6f} ETH")
                        logger.info(f"{'='*60}")
                        
                        return result
                except Exception as e:
                    logger.warning(f"从链上获取 Trace 失败: {e}")
                    logger.info("将尝试在 Anvil 中重新执行提案...")
            
            # 2. 确定 Fork 区块高度（提案创建前一刻）
            proposal_block = self.get_proposal_creation_block(proposal_data)
            if proposal_block is None:
                logger.error("无法确定提案创建的区块高度")
                logger.error("请确保提案数据包含 block_number 或 metadata.transaction_hash")
                return None
            
            # Fork 到提案创建前一刻（blockNumber - 1）
            fork_block = proposal_block - 1
            logger.info(f"提案创建区块: {proposal_block}")
            logger.info(f"Fork 到区块: {fork_block} (提案创建前一刻)")
            
            # 3. 启动 Anvil（Fork 到提案创建前一刻）
            if not self.start_anvil(fork_block=fork_block):
                logger.error("无法启动 Anvil，模拟终止")
                return None
            
            try:
                # 4. 执行提案（使用账户伪装）
                tx_hash = self.execute_proposal(proposal_data)
                if not tx_hash:
                    logger.error("提案执行失败")
                    logger.info("提示：如果提案已执行过，可以设置 use_existing_tx=True 直接从链上获取 Trace")
                    return None
                
                # 5. 获取 Trace（在同一进程中立即调用）
                logger.info("在同一进程中获取 Trace...")
                trace = self.get_trace(tx_hash)
                if not trace:
                    logger.error("获取 Trace 失败")
                    return None
                
                # 5. 提取调用和转账信息
                summary = self.extract_calls_and_transfers(trace)
                
                # 6. 构建完整结果
                result = {
                    "proposal_id": proposal_data.get("id"),
                    "proposal_title": proposal_data.get("title"),
                    "transaction_hash": tx_hash,
                    "source": "anvil",  # 标记来源为 Anvil 模拟
                    "trace": trace,  # 包含完整 trace
                    "summary": summary  # 包含提取的摘要
                }
                
                # 7. 保存结果
                if output_file is None:
                    proposal_id = str(proposal_data.get("id", "unknown"))
                    output_file = self.trace_dir / f"trace_summary_{proposal_id}.json"
                
                output_path = Path(output_file)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(output_path, 'w', encoding='utf-8') as f:
                    # 转换 AttributeDict 为普通字典
                    serializable_result = convert_to_serializable(result)
                    json.dump(serializable_result, f, indent=2, ensure_ascii=False)
                
                logger.success(f"✓ Trace 摘要已保存: {output_path}")
                
                # 打印摘要
                logger.info(f"{'='*60}")
                logger.info(f"Trace 摘要（来自 Anvil 模拟）:")
                logger.info(f"  总调用数: {summary['total_calls']}")
                logger.info(f"  总转账数: {summary['total_transfers']}")
                logger.info(f"  总转账金额: {summary['total_value_transferred_eth']:.6f} ETH")
                logger.info(f"{'='*60}")
                
                return result
                
            finally:
                # 确保停止 Anvil
                self.stop_anvil()
                
        except Exception as e:
            logger.error(f"模拟过程出错: {e}")
            logger.exception("详细错误:")
            self.stop_anvil()
            return None
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口（确保清理）"""
        self.stop_anvil()
        return False


def main():
    """主函数 - 模拟执行提案或重放交易"""
    
    # 配置日志
    logger.add(
        "logs/simulator_{time}.log",
        rotation="1 day",
        retention="7 days",
        level="DEBUG"
    )
    
    try:
        target_tx_hash = None
        
        # 检查命令行参数
        if len(sys.argv) > 1:
            # 如果提供了交易哈希作为参数，直接使用
            target_tx_hash = sys.argv[1]
            logger.info(f"从命令行参数获取交易哈希: {target_tx_hash}")
        else:
            # 如果没有提供参数，尝试从提案文件中读取
            proposal_file = "data/proposals/collected_proposal.json"
            
            if os.path.exists(proposal_file):
                logger.info(f"从提案文件读取交易哈希: {proposal_file}")
                try:
                    with open(proposal_file, 'r', encoding='utf-8') as f:
                        proposal_data = json.load(f)
                    
                    # 尝试从 metadata.transaction_hash 获取
                    if proposal_data.get("metadata", {}).get("transaction_hash"):
                        target_tx_hash = proposal_data["metadata"]["transaction_hash"]
                        logger.info(f"✓ 从提案文件获取交易哈希: {target_tx_hash}")
                    else:
                        logger.warning("提案文件中未找到 transaction_hash")
                except Exception as e:
                    logger.warning(f"读取提案文件失败: {e}")
            else:
                logger.warning(f"提案文件不存在: {proposal_file}")
        
        # 如果找到了交易哈希，使用 replay_transaction 模式
        if target_tx_hash:
            logger.info(f"{'='*60}")
            logger.info(f"使用交易重放模式")
            logger.info(f"交易哈希: {target_tx_hash}")
            logger.info(f"{'='*60}")
            
            # 创建模拟器
            simulator = ProposalSimulator()
            
            # 重放交易
            result = simulator.replay_transaction(target_tx_hash)
            
            if result:
                logger.success("✅ 交易重放完成！")
                return 0
            else:
                logger.error("❌ 交易重放失败")
                return 1
        else:
            # 如果没有交易哈希，使用传统的提案模拟模式
            proposal_file = "data/proposals/collected_proposal.json"
            
            if not os.path.exists(proposal_file):
                logger.error(f"提案文件不存在: {proposal_file}")
                logger.info("提示：")
                logger.info("1. 可以传入交易哈希作为参数来重放交易")
                logger.info("   用法: python simulator.py <交易哈希>")
                logger.info("2. 或者确保提案文件存在且包含 metadata.transaction_hash")
                return 1
            
            logger.info(f"{'='*60}")
            logger.info(f"使用提案模拟模式")
            logger.info(f"提案文件: {proposal_file}")
            logger.info(f"{'='*60}")
            
            # 创建模拟器
            # 从提案数据中读取 fork_block（如果存在）
            with open(proposal_file, 'r', encoding='utf-8') as f:
                proposal_data = json.load(f)
            
            fork_block = proposal_data.get("block_number")
            
            simulator = ProposalSimulator(fork_block=fork_block)
            
            # 执行模拟
            logger.info("开始模拟执行提案...")
            result = simulator.simulate_proposal(proposal_file)
            
            if result:
                logger.success("✅ 模拟完成！")
                return 0
            else:
                logger.error("❌ 模拟失败")
                return 1
    
    except Exception as e:
        logger.error(f"❌ 错误: {e}")
        logger.exception("详细堆栈:")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
