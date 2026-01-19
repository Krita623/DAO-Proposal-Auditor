#!/usr/bin/env python3
"""
Graph Builder - 图构建器

功能：
1. 读取 data/traces/trace_report.json
2. 使用 networkx 创建 MultiDiGraph（多重有向图）
3. 遍历 Trace，将 from 和 to 添加为节点
4. 将每一个 Call 添加为边，属性包括：type, function, value, depth
5. 特征提取：计算图的深度和广度，识别中心节点
6. 生成描述（Graph2Text）
7. 输出：保存图对象为 proposal_graph.gpickle，输出 graph_description.txt
8. 可视化：从 gpickle 文件加载图并生成可视化图片（PNG/SVG/PDF）
"""

import json
import pickle
import os
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from collections import Counter, defaultdict

import networkx as nx
from loguru import logger
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 已知合约地址库（用于语义识别）
KNOWN_CONTRACTS = {
    # Gnosis Safe 相关
    "0x3e5c63644e683549055b9be8653de26e0b4cd36e": "Gnosis Safe: Proxy Factory",
    "0xd9db270c1b5e3bd161e8c8503c55ceabee709552": "Gnosis Safe: Master Copy",
    "0xa6b71e26c5e0845f74c812102ca7114b6a896ab2": "Gnosis Safe: Proxy Factory v1.3.0",
    
    # Arbitrum Governor
    "0xf07ded9dc292157749b6fd268e37df6ea38395b9": "Arbitrum Governor",
    "0xb4c064f466931b8d0f637654c916e3f203c46f13": "Arbitrum Governor (Proposer)",
    
    # Uniswap Governor
    "0x408ed6354d4973f66138c91495f2f2fcbd8724c3": "Uniswap Governor",

    # 系统合约
    "0x0000000000000000000000000000000000000001": "Ethereum Precompile: ECRecover",
    "0x0000000000000000000000000000000000000002": "Ethereum Precompile: SHA256",
    "0x0000000000000000000000000000000000000003": "Ethereum Precompile: RIPEMD160",
    "0x0000000000000000000000000000000000000004": "Ethereum Precompile: Identity",
    "0x0000000000000000000000000000000000000005": "Ethereum Precompile: ModExp",
    "0x0000000000000000000000000000000000000064": "Arbitrum: L1 ArbSys",
    "0x0000000000000000000000000000000000000065": "Arbitrum: L2 ArbSys",
}

# 已知函数签名模式（用于识别合约类型）
FUNCTION_PATTERNS = {
    "execTransaction": "Gnosis Safe: Multi-sig execution",
    "propose": "Governor: Proposal creation",
    "execute": "Governor: Proposal execution",
    "castVote": "Governor: Voting",
    "getPastVotes": "Governor: Vote weight query",
    "delegate": "Governor: Delegation",
    "upgradeTo": "Proxy: Upgrade",
    "upgradeToAndCall": "Proxy: Upgrade and call",
}

# 尝试导入可视化库
try:
    import matplotlib
    matplotlib.use('Agg')  # 使用非交互式后端
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except (ImportError, ModuleNotFoundError) as e:
    MATPLOTLIB_AVAILABLE = False
    logger.warning(f"matplotlib not available: {e}")
    logger.warning("To enable graph visualization, install matplotlib: pip install matplotlib")
    plt = None

try:
    import pygraphviz
    PYGRAPHVIZ_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    PYGRAPHVIZ_AVAILABLE = False
    logger.debug("pygraphviz not available, will use matplotlib for visualization")


class GraphBuilder:
    """提案执行轨迹图构建器"""
    
    def __init__(self, trace_report_path: str = "data/traces/trace_report.json"):
        """
        初始化图构建器
        
        Args:
            trace_report_path: trace_report.json 文件路径
        """
        self.trace_report_path = Path(trace_report_path)
        self.graph: Optional[nx.MultiDiGraph] = None
        self.trace_data: Optional[Dict[str, Any]] = None
        
    def load_trace_report(self) -> Dict[str, Any]:
        """
        读取 trace_report.json
        
        Returns:
            解析后的 JSON 数据
        """
        if not self.trace_report_path.exists():
            raise FileNotFoundError(f"Trace report not found: {self.trace_report_path}")
        
        logger.info(f"Loading trace report from {self.trace_report_path}")
        with open(self.trace_report_path, 'r', encoding='utf-8') as f:
            self.trace_data = json.load(f)
        
        return self.trace_data
    
    def get_trace_summary(self) -> Dict[str, Any]:
        """
        获取 trace_summary 数据，兼容两种格式：
        - trace_summary（replay_transaction 模式）
        - summary（simulate_proposal 模式）
        
        Returns:
            trace_summary 字典
        """
        if self.trace_data is None:
            self.load_trace_report()
        
        # 优先使用 trace_summary，如果没有则使用 summary
        trace_summary = self.trace_data.get("trace_summary") or self.trace_data.get("summary", {})
        
        # 如果使用了 summary，记录日志以便调试
        if "summary" in self.trace_data and "trace_summary" not in self.trace_data:
            logger.debug("Using 'summary' field (simulate_proposal format) instead of 'trace_summary'")
        
        return trace_summary
    
    def build_graph(self) -> nx.MultiDiGraph:
        """
        构建 MultiDiGraph
        
        Returns:
            构建好的图对象
        """
        if self.trace_data is None:
            self.load_trace_report()
        
        logger.info("Building MultiDiGraph from trace data")
        self.graph = nx.MultiDiGraph()
        
        trace_summary = self.get_trace_summary()
        calls = trace_summary.get("calls", [])
        
        if not calls:
            logger.warning("No calls found in trace_summary/summary")
            return self.graph
        
        # 遍历所有调用，添加节点和边
        for call in calls:
            from_addr = call.get("from", "").lower()
            to_addr = call.get("to", "").lower()
            call_type = call.get("type", "CALL")
            function = call.get("function_signature", call.get("function_selector", "unknown"))
            value = call.get("value", 0)
            depth = call.get("depth", 0)
            
            # 跳过无效地址
            if not from_addr or not to_addr:
                continue
            
            # 添加节点（如果不存在）
            if not self.graph.has_node(from_addr):
                self.graph.add_node(from_addr, label=from_addr)
            
            if not self.graph.has_node(to_addr):
                self.graph.add_node(to_addr, label=to_addr)
            
            # 添加边（支持多重边）
            edge_attrs = {
                "type": call_type,
                "function": function,
                "value": value,
                "depth": depth
            }
            
            self.graph.add_edge(from_addr, to_addr, **edge_attrs)
        
        logger.info(f"Graph built: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")
        return self.graph
    
    def calculate_graph_depth(self) -> int:
        """
        计算图的深度（最长路径）
        
        Returns:
            图的深度
        """
        if self.graph is None:
            raise ValueError("Graph not built. Call build_graph() first.")
        
        if self.graph.number_of_nodes() == 0:
            return 0
        
        # 找到所有入度为 0 的节点（起始节点）
        in_degree_zero = [n for n in self.graph.nodes() if self.graph.in_degree(n) == 0]
        
        if not in_degree_zero:
            # 如果没有入度为 0 的节点，使用所有节点作为起点
            in_degree_zero = list(self.graph.nodes())
        
        max_depth = 0
        
        # 对每个起始节点，计算最长路径
        for start_node in in_degree_zero:
            try:
                # 使用 BFS 计算从该节点出发的最长路径
                depths = {start_node: 0}
                queue = [start_node]
                visited_in_path = set()  # 防止自环导致的无限循环
                
                while queue:
                    current = queue.pop(0)
                    current_depth = depths[current]
                    visited_in_path.add(current)
                    
                    # 遍历所有出边
                    for successor in self.graph.successors(current):
                        # 跳过自环，避免无限循环
                        if successor == current:
                            continue
                            
                        edge_depths = [self.graph[current][successor][key].get("depth", 0) 
                                     for key in self.graph[current][successor]]
                        max_edge_depth = max(edge_depths) if edge_depths else 0
                        
                        new_depth = current_depth + 1
                        # 如果节点已访问过且新深度不大于旧深度，跳过
                        if successor in visited_in_path and new_depth <= depths.get(successor, 0):
                            continue
                            
                        depths[successor] = new_depth
                        if successor not in visited_in_path:
                            queue.append(successor)
                    
                    max_depth = max(max_depth, current_depth)
            except Exception as e:
                logger.warning(f"Error calculating depth from {start_node}: {e}")
        
        return max_depth
    
    def calculate_graph_breadth(self) -> int:
        """
        计算图的广度（最大宽度，即同一层级最多节点数）
        
        Returns:
            图的广度
        """
        if self.graph is None:
            raise ValueError("Graph not built. Call build_graph() first.")
        
        if self.graph.number_of_nodes() == 0:
            return 0
        
        # 找到所有入度为 0 的节点（起始节点）
        in_degree_zero = [n for n in self.graph.nodes() if self.graph.in_degree(n) == 0]
        
        if not in_degree_zero:
            in_degree_zero = list(self.graph.nodes())
        
        max_breadth = 0
        
        # 使用 BFS 按层级遍历
        for start_node in in_degree_zero:
            try:
                levels = defaultdict(list)
                levels[0] = [start_node]
                visited = {start_node}
                queue = [(start_node, 0)]
                
                while queue:
                    current, level = queue.pop(0)
                    
                    # 遍历所有出边
                    for successor in self.graph.successors(current):
                        # 跳过自环，避免重复计算
                        if successor == current:
                            continue
                            
                        if successor not in visited:
                            visited.add(successor)
                            next_level = level + 1
                            levels[next_level].append(successor)
                            queue.append((successor, next_level))
                    
                    # 更新最大宽度
                    max_breadth = max(max_breadth, len(levels[level]))
            except Exception as e:
                logger.warning(f"Error calculating breadth from {start_node}: {e}")
        
        return max_breadth
    
    def identify_central_nodes(self, top_k: int = 5) -> List[Tuple[str, int]]:
        """
        识别中心节点（调用次数最多的合约）
        
        Args:
            top_k: 返回前 k 个中心节点
            
        Returns:
            [(节点地址, 调用次数), ...] 列表，按调用次数降序排列
        """
        if self.graph is None:
            raise ValueError("Graph not built. Call build_graph() first.")
        
        # 统计每个节点作为目标（被调用）的次数
        in_degree_counter = Counter()
        for node in self.graph.nodes():
            in_degree = self.graph.in_degree(node)
            if in_degree > 0:
                in_degree_counter[node] = in_degree
        
        # 返回前 k 个
        top_nodes = in_degree_counter.most_common(top_k)
        return top_nodes
    
    def identify_contract(self, address: str) -> Optional[str]:
        """
        识别已知合约地址
        
        Args:
            address: 合约地址
            
        Returns:
            合约名称，如果未知则返回 None
        """
        addr_lower = address.lower()
        return KNOWN_CONTRACTS.get(addr_lower)
    
    def identify_function_semantic(self, function_signature: str) -> Optional[str]:
        """
        识别函数签名的语义
        
        Args:
            function_signature: 函数签名
            
        Returns:
            函数语义描述，如果未知则返回 None
        """
        if not function_signature or function_signature == "unknown":
            return None
        
        # 提取函数名（签名格式：functionName(...)）
        func_name = function_signature.split("(")[0] if "(" in function_signature else function_signature
        
        for pattern, description in FUNCTION_PATTERNS.items():
            if pattern.lower() in func_name.lower():
                return description
        
        return None
    
    def extract_call_paths(self, max_paths: int = 3) -> List[List[Dict[str, Any]]]:
        """
        提取关键调用路径
        
        Args:
            max_paths: 最大路径数量
            
        Returns:
            调用路径列表
        """
        if self.graph is None or self.trace_data is None:
            return []
        
        trace_summary = self.get_trace_summary()
        calls = trace_summary.get("calls", [])
        
        # 按深度分组，提取关键路径
        paths_by_depth = defaultdict(list)
        for call in calls:
            depth = call.get("depth", 0)
            paths_by_depth[depth].append(call)
        
        # 选择最深路径和关键路径
        paths = []
        if paths_by_depth:
            max_depth = max(paths_by_depth.keys())
            # 选择最深路径的代表性调用
            if max_depth in paths_by_depth:
                paths.append(paths_by_depth[max_depth][:3])  # 取前3个
        
        # 选择包含重要函数的路径
        important_functions = ["execTransaction", "propose", "execute", "upgradeTo"]
        for call in calls:
            func = call.get("function_signature", "")
            if any(imp_func in func for imp_func in important_functions):
                if len(paths) < max_paths:
                    paths.append([call])
        
        return paths[:max_paths]
    
    def generate_description(self) -> str:
        """
        生成图描述文本（Graph2Text）- 增强版
        
        Returns:
            结构化的文本描述，包含函数签名、地址识别、调用路径等信息
        """
        if self.graph is None:
            raise ValueError("Graph not built. Call build_graph() first.")
        
        if self.trace_data is None:
            self.load_trace_report()
        
        # 获取基本信息
        num_nodes = self.graph.number_of_nodes()
        num_edges = self.graph.number_of_edges()
        graph_depth = self.calculate_graph_depth()
        graph_breadth = self.calculate_graph_breadth()
        central_nodes = self.identify_central_nodes(top_k=5)
        
        # 获取原始交易信息（兼容两种格式）
        original_tx = self.trace_data.get("original_transaction", {})
        trace_info = self.trace_data.get("trace", {})
        
        # 优先使用 original_transaction（replay_transaction 模式），否则使用 trace（simulate_proposal 模式）
        if original_tx:
            from_addr = original_tx.get("from", "unknown")
            to_addr = original_tx.get("to", "unknown")
        elif trace_info:
            from_addr = trace_info.get("from", "unknown")
            to_addr = trace_info.get("to", "unknown")
        else:
            # 如果都没有，尝试从第一个调用中获取
            trace_summary = self.get_trace_summary()
            calls = trace_summary.get("calls", [])
            if calls:
                first_call = calls[0]
                from_addr = first_call.get("from", "unknown")
                to_addr = first_call.get("to", "unknown")
            else:
                from_addr = "unknown"
                to_addr = "unknown"
        
        # 识别地址语义
        from_contract = self.identify_contract(from_addr)
        to_contract = self.identify_contract(to_addr)
        
        # 构建描述
        description_parts = []
        
        # 开头描述（包含地址识别）
        from_desc = from_contract if from_contract else f"地址 {from_addr}"
        to_desc = to_contract if to_contract else f"合约 {to_addr}"
        
        description_parts.append(
            f"该提案启动后，首先由 {from_desc} ({from_addr}) 调用了 {to_desc} ({to_addr})。"
        )
        
        # 提取关键函数调用信息
        trace_summary = self.get_trace_summary()
        calls = trace_summary.get("calls", [])
        
        # 统计函数调用
        function_calls = Counter()
        address_functions = defaultdict(set)  # 地址 -> 函数集合
        
        for call in calls:
            func = call.get("function_signature", call.get("function_selector", "unknown"))
            if func and func != "unknown":
                func_name = func.split("(")[0] if "(" in func else func
                function_calls[func_name] += 1
                
                to_addr_call = call.get("to", "").lower()
                if to_addr_call:
                    address_functions[to_addr_call].add(func_name)
        
        # 描述关键函数调用
        if function_calls:
            important_functions = []
            for func_name, count in function_calls.most_common(5):
                func_semantic = self.identify_function_semantic(func_name)
                if func_semantic:
                    important_functions.append(f"{func_name} ({func_semantic}, {count}次)")
                else:
                    important_functions.append(f"{func_name} ({count}次)")
            
            if important_functions:
                description_parts.append(f"关键函数调用包括：{', '.join(important_functions)}。")
        
        # 调用类型统计
        call_types = Counter()
        for u, v, data in self.graph.edges(data=True):
            call_type = data.get("type", "CALL")
            call_types[call_type] += 1
        
        if call_types:
            type_descriptions = []
            for call_type, count in call_types.items():
                type_descriptions.append(f"{call_type} {count} 次")
            description_parts.append(f"调用类型包括：{', '.join(type_descriptions)}。")
        
        # 中心节点描述（包含地址识别和函数信息）
        if central_nodes:
            central_desc = []
            for node, count in central_nodes:
                contract_name = self.identify_contract(node)
                if contract_name:
                    # 获取该地址调用的函数
                    funcs = address_functions.get(node.lower(), set())
                    func_info = f"，调用函数：{', '.join(list(funcs)[:3])}" if funcs else ""
                    central_desc.append(f"{contract_name} ({node})（被调用 {count} 次{func_info}）")
                else:
                    short_addr = f"{node[:10]}...{node[-8:]}" if len(node) > 18 else node
                    funcs = address_functions.get(node.lower(), set())
                    func_info = f"，调用函数：{', '.join(list(funcs)[:3])}" if funcs else ""
                    central_desc.append(f"合约 {short_addr} ({node})（被调用 {count} 次{func_info}）")
            description_parts.append(f"中心节点为：{', '.join(central_desc)}。")
        
        # 图结构描述
        description_parts.append(
            f"共涉及 {num_nodes} 个节点的 {num_edges} 次交互，"
            f"图的最大深度为 {graph_depth}，最大宽度为 {graph_breadth}。"
        )
        
        # 提取并描述关键调用路径
        call_paths = self.extract_call_paths(max_paths=2)
        if call_paths:
            path_descriptions = []
            for path in call_paths:
                if path:
                    first_call = path[0]
                    from_p = first_call.get("from", "")[:10] + "..."
                    to_p = first_call.get("to", "")[:10] + "..."
                    func_p = first_call.get("function_signature", "unknown")
                    func_name_p = func_p.split("(")[0] if "(" in func_p else func_p
                    depth_p = first_call.get("depth", 0)
                    
                    to_contract_p = self.identify_contract(first_call.get("to", ""))
                    to_desc_p = to_contract_p if to_contract_p else to_p
                    
                    path_descriptions.append(f"{from_p} -> {to_desc_p} 调用 {func_name_p} (深度 {depth_p})")
            
            if path_descriptions:
                description_parts.append(f"关键调用路径：{'; '.join(path_descriptions)}。")
        
        # 特殊调用类型描述（增强版）
        if "DELEGATECALL" in call_types:
            # 查找 DELEGATECALL 的具体用途
            delegatecall_info = []
            for call in calls:
                if call.get("type") == "DELEGATECALL":
                    to_addr_dc = call.get("to", "")
                    contract_dc = self.identify_contract(to_addr_dc)
                    if contract_dc:
                        delegatecall_info.append(contract_dc)
            
            if delegatecall_info:
                description_parts.append(
                    f"该提案使用了 DELEGATECALL 机制，涉及 {', '.join(set(delegatecall_info))}，"
                    "表明存在代理合约模式，核心逻辑通过委托调用触达实现合约。"
                )
            else:
                description_parts.append(
                    "该提案使用了 DELEGATECALL 机制，表明存在代理合约模式，"
                    "核心逻辑通过委托调用触达实现合约。"
                )
        
        if "STATICCALL" in call_types:
            description_parts.append(
                "该提案包含 STATICCALL 调用，用于读取合约状态而不修改链上数据。"
            )
        
        # 识别治理流程模式
        has_exec_transaction = any("execTransaction" in str(call.get("function_signature", "")) for call in calls)
        has_propose = any("propose" in str(call.get("function_signature", "")).lower() for call in calls)
        
        if has_exec_transaction and has_propose:
            description_parts.append(
                "执行轨迹显示这是标准的 DAO 治理流程：通过多签钱包（Gnosis Safe）执行交易，"
                "调用 Governor 合约创建提案。这是提案创建阶段，而非提案执行阶段。"
            )
        elif has_exec_transaction:
            description_parts.append(
                "执行轨迹包含多签执行（execTransaction），表明这是通过多签钱包发起的操作。"
            )
        elif has_propose:
            description_parts.append(
                "执行轨迹包含提案创建（propose），表明这是 DAO 治理提案的创建流程。"
            )
        
        # 组合完整描述
        full_description = " ".join(description_parts)
        
        return full_description
    
    def save_graph(self, output_path: str = "outputs/proposal_graph.gpickle"):
        """
        保存图对象为 gpickle 格式
        
        Args:
            output_path: 输出文件路径
        """
        if self.graph is None:
            raise ValueError("Graph not built. Call build_graph() first.")
        
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Saving graph to {output_file}")
        with open(output_file, 'wb') as f:
            pickle.dump(self.graph, f)
        
        logger.info(f"Graph saved: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")
    
    def save_description(self, output_path: str = "outputs/graph_description.txt"):
        """
        保存图描述文本
        
        Args:
            output_path: 输出文件路径
        """
        description = self.generate_description()
        
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Saving description to {output_file}")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(description)
        
        logger.info("Description saved")
    
    def load_graph(self, graph_path: str) -> nx.MultiDiGraph:
        """
        从 gpickle 文件加载图对象
        
        Args:
            graph_path: gpickle 文件路径
            
        Returns:
            加载的图对象
        """
        graph_file = Path(graph_path)
        if not graph_file.exists():
            raise FileNotFoundError(f"Graph file not found: {graph_path}")
        
        logger.info(f"Loading graph from {graph_file}")
        with open(graph_file, 'rb') as f:
            self.graph = pickle.load(f)
        
        logger.info(f"Graph loaded: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")
        return self.graph
    
    def visualize_graph(self, 
                       output_path: str = "outputs/proposal_graph.png",
                       format: str = "png",
                       layout: str = "spring",
                       node_size: int = 300,
                       font_size: int = 8,
                       figsize: Tuple[int, int] = (16, 12),
                       dpi: int = 300) -> bool:
        """
        生成图的可视化图片
        
        Args:
            output_path: 输出图片路径
            format: 输出格式 (png, svg, pdf)
            layout: 布局算法 (spring, circular, kamada_kawai, planar, shell)
            node_size: 节点大小
            font_size: 字体大小
            figsize: 图片尺寸 (宽, 高)
            dpi: 图片分辨率
            
        Returns:
            是否成功生成
        """
        if self.graph is None:
            raise ValueError("Graph not loaded. Call load_graph() or build_graph() first.")
        
        if not MATPLOTLIB_AVAILABLE:
            logger.error("matplotlib not available, cannot generate visualization")
            logger.error("Please install matplotlib: pip install matplotlib")
            raise ImportError(
                "matplotlib is required for graph visualization. "
                "Install it with: pip install matplotlib"
            )
        
        if self.graph.number_of_nodes() == 0:
            logger.warning("Graph is empty, cannot visualize")
            return False
        
        logger.info(f"Generating graph visualization: {output_path}")
        
        # 配置字体（使用英文，无需特殊字体配置）
        plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题
        
        # 根据格式确定文件扩展名
        output_file = Path(output_path)
        if not output_file.suffix:
            output_file = output_file.with_suffix(f".{format}")
        
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 创建图形，使用浅色背景主题
        fig, ax = plt.subplots(figsize=figsize, dpi=dpi, facecolor='white')
        ax.set_facecolor('white')
        
        # 计算布局（使用更好的参数）
        try:
            if layout == "spring":
                pos = nx.spring_layout(self.graph, k=2, iterations=100, seed=42)
            elif layout == "circular":
                pos = nx.circular_layout(self.graph)
            elif layout == "kamada_kawai":
                pos = nx.kamada_kawai_layout(self.graph)
            elif layout == "planar":
                try:
                    pos = nx.planar_layout(self.graph)
                except:
                    pos = nx.spring_layout(self.graph, k=2, iterations=100)
            elif layout == "shell":
                pos = nx.shell_layout(self.graph)
            else:
                pos = nx.spring_layout(self.graph, k=2, iterations=100)
        except Exception as e:
            logger.warning(f"Layout algorithm {layout} failed, using spring layout: {e}")
            pos = nx.spring_layout(self.graph, k=2, iterations=100)
        
        # 计算节点颜色（使用更美观的配色方案）
        node_colors = []
        node_sizes = []
        in_degrees = dict(self.graph.in_degree())
        out_degrees = dict(self.graph.out_degree())
        max_in_degree = max(in_degrees.values()) if in_degrees else 1
        max_out_degree = max(out_degrees.values()) if out_degrees else 1
        
        # 使用渐变色：从浅蓝到深蓝再到紫色
        try:
            # 使用新的 matplotlib API（3.7+）
            try:
                colormap = matplotlib.colormaps['viridis']
            except (AttributeError, KeyError):
                # 兼容旧版本
                from matplotlib import cm
                if hasattr(cm, 'get_cmap'):
                    colormap = cm.get_cmap('viridis')
                else:
                    colormap = cm.viridis
        except (ImportError, AttributeError):
            colormap = None
        
        for node in self.graph.nodes():
            in_deg = in_degrees.get(node, 0)
            out_deg = out_degrees.get(node, 0)
            # 根据入度和出度的综合值确定颜色
            total_importance = (in_deg * 0.7 + out_deg * 0.3) / max(max_in_degree, max_out_degree, 1)
            # 使用 viridis 配色方案（从黄绿色到紫色）
            if colormap:
                node_colors.append(colormap(0.2 + total_importance * 0.6))
            else:
                # 回退到简单的渐变色（蓝紫色系）
                intensity = 0.3 + total_importance * 0.7
                node_colors.append((0.2, 0.4 + intensity * 0.4, 0.8))
            # 节点大小根据重要性调整
            base_size = node_size
            importance_factor = 1 + total_importance * 0.5
            node_sizes.append(int(base_size * importance_factor))
        
        # 绘制节点（添加边框，适应浅色背景）
        nodes = nx.draw_networkx_nodes(
            self.graph, 
            pos, 
            node_color=node_colors,
            node_size=node_sizes,
            alpha=0.8,
            linewidths=2,
            edgecolors='#2c3e50',  # 深色边框
            ax=ax
        )
        
        # 绘制边（根据调用类型使用不同颜色和样式，适应浅色背景）
        edge_colors = []
        edge_styles = []
        edge_widths = []
        
        for u, v, data in self.graph.edges(data=True):
            call_type = data.get("type", "CALL")
            if call_type == "DELEGATECALL":
                edge_colors.append("#e74c3c")  # 深红色
                edge_styles.append("dashed")
                edge_widths.append(2.5)
            elif call_type == "STATICCALL":
                edge_colors.append("#3498db")  # 深蓝色
                edge_styles.append("dotted")
                edge_widths.append(2.0)
            else:  # CALL
                edge_colors.append("#34495e")  # 深灰色
                edge_styles.append("solid")
                edge_widths.append(2.0)
        
        # 绘制边（分组绘制以支持不同样式）
        edge_groups = {"solid": [], "dashed": [], "dotted": []}
        edge_color_groups = {"solid": [], "dashed": [], "dotted": []}
        edge_width_groups = {"solid": [], "dashed": [], "dotted": []}
        
        for (u, v, data), color, style, width in zip(
            self.graph.edges(data=True), edge_colors, edge_styles, edge_widths
        ):
            edge_groups[style].append((u, v))
            edge_color_groups[style].append(color)
            edge_width_groups[style].append(width)
        
        for style in ["solid", "dashed", "dotted"]:
            if edge_groups[style]:
                nx.draw_networkx_edges(
                    self.graph,
                    pos,
                    edgelist=edge_groups[style],
                    edge_color=edge_color_groups[style],
                    width=edge_width_groups[style],
                    alpha=0.8,  # 提高透明度，在浅色背景上更清晰
                    style=style,
                    arrows=True,
                    arrowsize=20,
                    arrowstyle='->',
                    connectionstyle='arc3,rad=0.1',
                    ax=ax
                )
        
        # 绘制节点标签（缩短地址显示，使用偏移避免与箭头重叠）
        labels = {}
        label_pos = {}
        
        # 计算标签偏移位置，避免与箭头重叠
        import math
        for node in self.graph.nodes():
            if len(node) > 18:
                labels[node] = f"{node[:6]}...{node[-4:]}"
            else:
                labels[node] = node
            
            # 计算节点的平均出边方向，将标签放在相反方向
            x, y = pos[node]
            out_edges = list(self.graph.out_edges(node))
            in_edges = list(self.graph.in_edges(node))
            
            if out_edges or in_edges:
                # 计算所有连接边的平均方向
                dx, dy = 0, 0
                for u, v in out_edges:
                    vx, vy = pos[v]
                    dx += (vx - x)
                    dy += (vy - y)
                for u, v in in_edges:
                    ux, uy = pos[u]
                    dx += (x - ux)
                    dy += (y - uy)
                
                # 归一化方向向量
                total_edges = len(out_edges) + len(in_edges)
                if total_edges > 0:
                    dx /= total_edges
                    dy /= total_edges
                    norm = math.sqrt(dx*dx + dy*dy)
                    if norm > 0:
                        dx /= norm
                        dy /= norm
                
                # 标签放在相反方向，距离节点更远
                offset = 0.15  # 偏移距离
                label_pos[node] = (x - dx * offset, y - dy * offset)
            else:
                # 如果没有边，标签放在节点下方
                label_pos[node] = (x, y - 0.12)
        
        # 使用偏移位置绘制标签
        for node, label_text in labels.items():
            x, y = label_pos[node]
            ax.text(
                x, y, label_text,
                fontsize=font_size + 2,
                fontweight="bold",
                color='#2c3e50',
                ha='center',
                va='center',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.95, edgecolor='#34495e', linewidth=1.5)
            )
        
        # 添加图例（避免与线条重叠，使用浅色背景样式）
        from matplotlib.patches import Patch
        from matplotlib.lines import Line2D
        
        legend_elements = [
            Line2D([0], [0], color='#34495e', lw=2, label='CALL'),
            Line2D([0], [0], color='#e74c3c', lw=2.5, linestyle='--', label='DELEGATECALL'),
            Line2D([0], [0], color='#3498db', lw=2, linestyle=':', label='STATICCALL'),
        ]
        # 将图例放在右上角
        legend = ax.legend(
            handles=legend_elements, 
            loc='upper right',
            frameon=True,
            fancybox=True,
            shadow=True,
            framealpha=0.95,
            facecolor='white',
            edgecolor='#34495e',
            labelcolor='#2c3e50',
            fontsize=11
        )
        
        # 设置标题（适应浅色背景）
        ax.set_title(
            f"Proposal Execution Trace Graph\nNodes: {self.graph.number_of_nodes()} | Edges: {self.graph.number_of_edges()} | Depth: {self.calculate_graph_depth()}",
            fontsize=16,
            fontweight="bold",
            color='#2c3e50',
            pad=20,
            bbox=dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.95, edgecolor='#34495e', linewidth=2)
        )
        
        ax.axis('off')
        plt.tight_layout()
        
        # 保存图片
        plt.savefig(output_file, format=format, dpi=dpi, bbox_inches='tight')
        plt.close()
        
        logger.info(f"Graph visualization saved to {output_file}")
        return True
    
    def run(self, 
            graph_output: str = "outputs/proposal_graph.gpickle",
            description_output: str = "outputs/graph_description.txt"):
        """
        运行完整的图构建流程
        
        Args:
            graph_output: 图对象输出路径
            description_output: 描述文本输出路径
        """
        logger.info("Starting graph building process")
        
        # 1. 加载数据
        self.load_trace_report()
        
        # 2. 构建图
        self.build_graph()
        
        # 3. 保存图对象
        self.save_graph(graph_output)
        
        # 4. 生成并保存描述
        self.save_description(description_output)
        
        # 5. 生成可视化（从环境变量读取配置）
        enable_viz = os.getenv("ENABLE_GRAPH_VISUALIZATION", "False").lower() in ("true", "1", "yes")
        
        if enable_viz:
            # 检查 matplotlib 是否可用
            if not MATPLOTLIB_AVAILABLE:
                logger.error("Graph visualization is enabled but matplotlib is not available")
                logger.error("Please install matplotlib: pip install matplotlib")
                logger.warning("Skipping graph visualization")
            else:
                # 从环境变量读取格式，输出目录固定为 outputs
                viz_format = os.getenv("GRAPH_OUTPUT_FORMAT", "png").lower()
                
                # 根据图输出路径生成可视化输出路径（固定使用 outputs 目录）
                base_name = Path(graph_output).stem
                visualization_output = f"outputs/{base_name}.{viz_format}"
                
                try:
                    self.visualize_graph(
                        output_path=visualization_output,
                        format=viz_format
                    )
                    logger.info(f"Graph visualization saved to {visualization_output}")
                except Exception as e:
                    logger.error(f"Failed to generate visualization: {e}")
        
        logger.info("Graph building process completed")
        
        return self.graph


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="构建提案执行轨迹图")
    parser.add_argument(
        "--input",
        type=str,
        default="data/traces/trace_report.json",
        help="输入 trace_report.json 文件路径"
    )
    parser.add_argument(
        "--graph-output",
        type=str,
        default="outputs/proposal_graph.gpickle",
        help="图对象输出路径"
    )
    parser.add_argument(
        "--description-output",
        type=str,
        default="outputs/graph_description.txt",
        help="描述文本输出路径"
    )
    
    args = parser.parse_args()
    
    builder = GraphBuilder(trace_report_path=args.input)
    graph = builder.run(
        graph_output=args.graph_output,
        description_output=args.description_output
    )
    
    # 打印统计信息（使用 logger 避免重复输出）
    logger.info("=" * 60)
    logger.info("图构建完成！")
    logger.info(f"节点数: {graph.number_of_nodes()}")
    logger.info(f"边数: {graph.number_of_edges()}")
    logger.info(f"图深度: {builder.calculate_graph_depth()}")
    logger.info(f"图广度: {builder.calculate_graph_breadth()}")
    central_nodes = builder.identify_central_nodes(top_k=3)
    if central_nodes:
        logger.info(f"中心节点: {', '.join([f'{addr[:10]}...{addr[-4:]} ({count}次)' for addr, count in central_nodes])}")
    logger.info(f"描述文本已保存到: {args.description_output}")
    
    # 检查是否生成了可视化
    enable_viz = os.getenv("ENABLE_GRAPH_VISUALIZATION", "False").lower() in ("true", "1", "yes")
    if enable_viz:
        viz_format = os.getenv("GRAPH_OUTPUT_FORMAT", "png").lower()
        base_name = Path(args.graph_output).stem
        viz_path = f"outputs/{base_name}.{viz_format}"
        if Path(viz_path).exists():
            logger.info(f"可视化图片已保存到: {viz_path}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
