#!/usr/bin/env python3
"""
Proposal Collector - 提案数据收集器

从链上收集 DAO 提案数据，应用硬规则过滤可执行提案。

硬规则（Executable Proposal）：
- targets 长度 > 0，或
- values 包含非零值，或
- calldatas 包含非空字节流（不是 0x）

满足任一条件即为"可执行提案"，否则为"社交提案"（不收集）。
"""

import json
import os
from typing import Dict, List, Optional, Any
from datetime import datetime
from web3 import Web3
from web3.exceptions import BlockNotFound
from dotenv import load_dotenv
from loguru import logger

# 加载环境变量
load_dotenv()


# Compound Governor Bravo ABI（简化版）
GOVERNOR_BRAVO_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": False, "internalType": "uint256", "name": "id", "type": "uint256"},
            {"indexed": False, "internalType": "address", "name": "proposer", "type": "address"},
            {"indexed": False, "internalType": "address[]", "name": "targets", "type": "address[]"},
            {"indexed": False, "internalType": "uint256[]", "name": "values", "type": "uint256[]"},
            {"indexed": False, "internalType": "string[]", "name": "signatures", "type": "string[]"},
            {"indexed": False, "internalType": "bytes[]", "name": "calldatas", "type": "bytes[]"},
            {"indexed": False, "internalType": "uint256", "name": "startBlock", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "endBlock", "type": "uint256"},
            {"indexed": False, "internalType": "string", "name": "description", "type": "string"}
        ],
        "name": "ProposalCreated",
        "type": "event"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "proposalId", "type": "uint256"}],
        "name": "proposals",
        "outputs": [
            {"internalType": "uint256", "name": "id", "type": "uint256"},
            {"internalType": "address", "name": "proposer", "type": "address"},
            {"internalType": "uint256", "name": "eta", "type": "uint256"},
            {"internalType": "uint256", "name": "startBlock", "type": "uint256"},
            {"internalType": "uint256", "name": "endBlock", "type": "uint256"},
            {"internalType": "uint256", "name": "forVotes", "type": "uint256"},
            {"internalType": "uint256", "name": "againstVotes", "type": "uint256"},
            {"internalType": "uint256", "name": "abstainVotes", "type": "uint256"},
            {"internalType": "bool", "name": "canceled", "type": "bool"},
            {"internalType": "bool", "name": "executed", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]


class ProposalCollector:
    """提案数据收集器"""
    
    # DAO Governor 合约地址
    # Compound Governor Bravo: 0xc0Da02939E1441F497fd74F78cE7Decb17B66529
    # Arbitrum Governor: 0xf07DeD9dC292157749B6Fd268E37DF6EA38395B9
    # Uniswap Governor: 0x408ED6354d4973f66138C91495F2f2FCbd8724C3
    GOVERNOR_ADDRESS = "0x408ED6354d4973f66138C91495F2f2FCbd8724C3"  # 修改为对应链的 Governor 地址
    

    # Alchemy 免费版限制：eth_getLogs 最多查询 10 个区块（适用于所有 EVM 链）
    # 参考：https://www.alchemy.com/docs/chains/ethereum/ethereum-api-endpoints/eth-get-logs
    BATCH_SIZE = 10
    
    def __init__(self, rpc_url: Optional[str] = None):
        """
        初始化收集器
        
        Args:
            rpc_url: RPC URL，如果为 None 则从环境变量读取
        """
        # Arbitrum 使用 ARBITRUM_RPC_URL，Ethereum 使用 MAINNET_RPC_URL
        self.rpc_url = rpc_url or os.getenv("ARBITRUM_RPC_URL") or os.getenv("MAINNET_RPC_URL")
        
        if not self.rpc_url or "YOUR_API_KEY" in self.rpc_url:
            raise ValueError(
                "请在 .env 文件中配置 ARBITRUM_RPC_URL 或 MAINNET_RPC_URL\n"
                "获取方法：访问 https://www.alchemy.com/ 注册并创建应用"
            )
        
        # 连接 Web3
        logger.info(f"连接到区块链节点...")
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        if not self.w3.is_connected():
            raise ConnectionError("无法连接到节点，请检查 RPC URL")
        
        logger.success(f"✓ 已连接，当前区块: {self.w3.eth.block_number:,}")
        
        # 初始化合约
        self.governor = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.GOVERNOR_ADDRESS),
            abi=GOVERNOR_BRAVO_ABI
        )
    
    def is_executable_proposal(
        self, 
        targets: List[str], 
        values: List[int], 
        calldatas: List[bytes]
    ) -> bool:
        """
        判断是否为可执行提案（硬规则）
        
        Args:
            targets: 目标合约地址数组
            values: ETH 转账金额数组
            calldatas: 函数调用数据数组
            
        Returns:
            True: 可执行提案Executable Proposal（需要收集）
            False: 社交提案Social Proposal（不收集）
        """
        # 规则 1: targets 非空
        if len(targets) > 0:
            return True
        
        # 规则 2: values 包含非零值
        if any(v > 0 for v in values):
            return True
        
        # 规则 3: calldatas 包含非空数据
        for calldata in calldatas:
            if calldata and calldata != b'' and calldata.hex() != '0x':
                return True
        
        # 所有条件都不满足，为社交提案
        return False
    
    def extract_proposal_from_event(self, event: Dict) -> Optional[Dict[str, Any]]:
        """
        从 ProposalCreated 事件中提取提案数据
        
        Args:
            event: Web3 事件对象
            
        Returns:
            提案数据字典，如果不是可执行提案则返回 None
        """
        args = event['args']
        
        proposal_id = args['id']
        proposer = args['proposer']
        targets = args['targets']
        values = args['values']
        calldatas = args['calldatas']
        start_block = args['startBlock']
        end_block = args['endBlock']
        description = args['description']
        
        logger.info(f"{'='*60}")
        logger.info(f"发现提案 #{str(proposal_id)[:20]}...")
        logger.info(f"targets: {len(targets)} | calldatas: {len(calldatas)} | values: {values}")
        
        # 应用硬规则过滤
        if not self.is_executable_proposal(targets, values, calldatas):
            logger.warning(f"⊘ 社交提案，跳过")
            return None
        
        logger.success(f"✓ 可执行提案，收集中...")
        
        # 提取提案标题（从描述的第一行）
        title = description.split('\n')[0].strip()
        if len(title) > 100:
            title = title[:100] + "..."
        
        # 获取区块信息
        try:
            block = self.w3.eth.get_block(event['blockNumber'])
            block_timestamp = block['timestamp']
        except Exception as e:
            logger.warning(f"获取区块时间戳失败: {e}")
            block_timestamp = None
        
        # 构造提案数据
        proposal_data = {
            "id": proposal_id,  # 保留原始 ID，不添加前缀
            "title": title,
            "description": description,
            "proposer": proposer,
            "targets": targets,
            "values": [int(v) for v in values],  # 转换为普通 int
            "calldatas": [cd.hex() for cd in calldatas],  # 转换为 hex 字符串
            # "chain": "arbitrum",  # 当前收集的是 Arbitrum 链提案
            "chain": "ethereum",  # 改为 ethereum（Uniswap 在以太坊主网）
            "block_number": event['blockNumber'],
            "metadata": {
                "voting_start_block": start_block,
                "voting_end_block": end_block,
                "created_timestamp": block_timestamp,
                "transaction_hash": event['transactionHash'].hex()
            }
        }
        
        return proposal_data
    
    def collect_one(
        self, 
        proposal_id: Optional[int] = None,
        from_block: Optional[int] = None,
        to_block: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        收集单个提案（用于测试）
        
        Args:
            proposal_id: 指定提案 ID（如果为 None 则收集第一个可执行提案）
            from_block: 起始区块（如果为 None 则从当前块向前查询 10000 个区块）
            to_block: 结束区块（如果为 None 则使用最新块）
            
        Returns:
            提案数据字典，如果没有找到则返回 None
        """
        if to_block is None:
            to_block = self.w3.eth.block_number
        
        if from_block is None:
            from_block = max(1, to_block - 10000)  # 默认查询最近 10000 个区块

        logger.info(f"{'='*60}")
        logger.info(f"扫描区块: {from_block:,} -> {to_block:,}")
        logger.info(f"{'='*60}")
        
        # 创建事件过滤器
        try:
            logger.info("查询 ProposalCreated 事件...")
            
            # 分批查询（避免请求过大）
            # 使用类常量 BATCH_SIZE（Alchemy 免费版限制）
            current_block = from_block
            
            while current_block <= to_block:
                # 注意：区块范围 [A, B] 是闭区间，包含 B-A+1 个区块
                # 所以 BATCH_SIZE=10 时，应该是 [current, current+9]，共 10 个区块
                batch_end = min(current_block + self.BATCH_SIZE - 1, to_block)
                
                logger.debug(f"查询区块 {current_block:,} -> {batch_end:,}")
                
                # 使用 get_logs() 替代 create_filter()（更稳定）
                events = self.governor.events.ProposalCreated.get_logs(
                    from_block=current_block,
                    to_block=batch_end
                )
                
                if len(events) > 0:
                    logger.info(f"✓ 找到 {len(events)} 个事件")
                
                # 遍历事件
                for event in events:
                    proposal_data = self.extract_proposal_from_event(event)
                    
                    if proposal_data:
                        # 如果指定了 proposal_id，检查是否匹配
                        if proposal_id is not None:
                            event_id = event['args']['id']
                            if event_id == proposal_id:
                                return proposal_data
                        else:
                            # 没有指定 ID，返回第一个可执行提案
                            return proposal_data
                
                current_block = batch_end + 1
            
            logger.warning("未找到匹配提案")
            return None
            
        except Exception as e:
            logger.error(f"❌ 失败: {e}")
            raise
    
    def save_proposal(self, proposal_data: Dict[str, Any], output_file: str = "proposal_data.json"):
        """
        保存提案数据到 JSON 文件
        
        Args:
            proposal_data: 提案数据字典
            output_file: 输出文件路径
        """
        # 确保输出目录存在
        output_dir = os.path.dirname(output_file)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # 保存为格式化的 JSON
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(proposal_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"{'='*60}")
        logger.success(f"✓ 已保存: {output_file}")
        
        # 打印摘要
        logger.info(f"标题: {proposal_data['title']}")
        logger.info(f"链: {proposal_data['chain']} | 区块: {proposal_data['block_number']:,}")
        logger.info(f"{'='*60}\n")


def main():
    """主函数 - 收集单个提案作为测试"""
    
    # 配置日志
    logger.add(
        "logs/collector_{time}.log",
        rotation="1 day",
        retention="7 days",
        level="DEBUG"
    )
    
    try:
        # 初始化收集器
        collector = ProposalCollector()
        
        logger.info(f"开始收集提案...")

        # 收集 Arbitrum DAO 提案
        # proposal_id = 53154361738756237993090798888616593723057470462495169047773178676976253908001
        proposal_id = 92
        
        # proposal_data = collector.collect_one(
        #     proposal_id=proposal_id,    # 提案 ID
        #     from_block=406178370,       # 提案创建区块号
        #     to_block=406178389
        # )
        proposal_data = collector.collect_one(
            proposal_id=proposal_id,    # 提案 ID
            from_block=24027640,       # 提案创建区块号
            to_block=24027649
        )
        
        if proposal_data:
            output_file = "data/proposals/collected_proposal.json"
            collector.save_proposal(proposal_data, output_file)
            logger.success("✅ 收集完成！")
        else:
            logger.warning("⚠️  未找到可执行提案")
    
    except Exception as e:
        logger.error(f"❌ 错误: {e}")
        logger.exception("详细堆栈:")
        return 1
    
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
