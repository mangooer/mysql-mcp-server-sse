"""
MySQL数据库信息查询工具
提供数据库、变量和状态等系统信息查询功能
"""

import json
import logging
import re
import os
from typing import Any, Dict, List, Optional
from mcp.server.fastmcp import FastMCP

from .metadata_base_tool import MetadataToolBase, ParameterValidationError, QueryExecutionError
from src.security.sql_analyzer import EnvironmentType
from src.security.database_scope_checker import create_database_checker
from src.db.mysql_operations import get_db_connection, execute_query
from src.validators import SQLValidators
from src.config import SecurityConfig, DatabaseConfig

logger = logging.getLogger("mysql_server")

# 自定义异常类
class SecurityError(QueryExecutionError):
    """安全限制错误"""
    pass

# 从环境变量读取敏感字段列表
def get_sensitive_patterns():
    """从环境变量获取敏感字段模式列表"""
    default_patterns = [
        r'password', r'auth', r'credential', r'key', r'secret', r'private', 
        r'host', r'path', r'directory', r'ssl', r'iptables', r'filter'
    ]
    
    env_patterns = os.getenv('SENSITIVE_INFO_FIELDS', '')
    if env_patterns:
        # 合并自定义模式和默认模式
        patterns = [pattern.strip() for pattern in env_patterns.split(',') if pattern.strip()]
        return list(set(patterns + default_patterns))
    
    return default_patterns

# 敏感变量和状态关键字列表
SENSITIVE_VARIABLE_PATTERNS = get_sensitive_patterns()

# 敏感变量名前缀，生产环境中这些变量的值会被隐藏
SENSITIVE_VARIABLE_PREFIXES = [
    "password", "auth", "secret", "key", "certificate", "ssl", "tls", "cipher", 
    "authentication", "secure", "credential", "token"
]

# 变量名和值字段映射
VARIABLE_NAME_FIELDS = ['Variable_name', 'variable_name', 'name', 'Name', 'key', 'Key', 'Setting']
VALUE_FIELDS = ['Value', 'value', 'variable_value', 'val', 'setting', 'Setting_Value']

def check_environment_permission(env_type: EnvironmentType, query_type: str) -> bool:
    """
    检查当前环境是否允许执行特定类型的查询
    
    Args:
        env_type: 环境类型（开发/生产）
        query_type: 查询类型
        
    Returns:
        bool: 是否允许执行
    """
    # 开发环境不限制查询
    if env_type == EnvironmentType.DEVELOPMENT:
        return True
        
    # 生产环境限制敏感信息查询
    sensitive_queries = ['variables', 'status', 'processlist']
    
    if query_type in sensitive_queries:
        # 检查是否在环境变量中明确允许
        allow_sensitive = os.getenv('ALLOW_SENSITIVE_INFO', 'false').lower() == 'true'
        if not allow_sensitive:
            logger.warning(f"生产环境中禁止执行敏感查询: {query_type}")
            return False
            
    return True

def filter_sensitive_info(results: List[Dict[str, Any]], filter_patterns: List[str] = None) -> List[Dict[str, Any]]:
    """
    过滤结果中的敏感信息
    
    Args:
        results: 查询结果
        filter_patterns: 敏感信息的正则表达式模式列表
        
    Returns:
        过滤后的结果列表
    """
    if not filter_patterns:
        filter_patterns = SENSITIVE_VARIABLE_PATTERNS
        
    filtered_results = []
    for item in results:
        # 复制一份，避免修改原始数据
        filtered_item = item.copy()
        
        # 确定哪个字段包含变量名
        name_field = None
        for field in VARIABLE_NAME_FIELDS:
            if field in filtered_item:
                name_field = field
                break
                
        # 如果找到变量名字段，检查是否敏感
        if name_field:
            var_name = str(filtered_item[name_field]).lower()
            # 检查是否匹配敏感模式
            is_sensitive = any(re.search(pattern, var_name, re.IGNORECASE) for pattern in filter_patterns)
            
            if is_sensitive:
                # 找出所有可能的值字段
                for value_field in VALUE_FIELDS:
                    if value_field in filtered_item:
                        # 敏感信息，隐藏具体的值
                        filtered_item[value_field] = '*** HIDDEN ***'
                        logger.debug(f"已隐藏敏感变量 '{var_name}' 的值")
                        
        filtered_results.append(filtered_item)
        
    return filtered_results

def register_info_tools(mcp: FastMCP):
    """
    注册MySQL数据库信息查询工具到MCP服务器
    
    Args:
        mcp: FastMCP服务器实例
    """
    logger.debug("注册MySQL数据库信息查询工具...")
    
    # 创建数据库范围检查器
    database_checker = None
    if SecurityConfig.ENABLE_DATABASE_ISOLATION and DatabaseConfig.DATABASE:
        database_checker = create_database_checker(
            allowed_database=DatabaseConfig.DATABASE,
            access_level=SecurityConfig.DATABASE_ACCESS_LEVEL
        )
    
    @mcp.tool()
    @MetadataToolBase.handle_query_error
    async def mysql_show_databases(pattern: Optional[str] = None, limit: int = 100, exclude_system: bool = True) -> str:
        """
        获取所有数据库列表，支持筛选和限制结果数量
        
        Args:
            pattern: 数据库名称匹配模式 (可选, 例如 '%test%')
            limit: 返回结果的最大数量 (默认100，设为0表示无限制)
            exclude_system: 是否排除系统数据库 (默认为True)
            
        Returns:
            数据库列表的JSON字符串
        """
        # 参数验证
        if pattern:
            MetadataToolBase.validate_parameter(
                "pattern", pattern,
                SQLValidators.validate_like_pattern,
                "模式只能包含字母、数字、下划线和通配符(%_)"
            )
        
        MetadataToolBase.validate_parameter(
            "limit", limit,
            lambda x: SQLValidators.validate_integer(x, min_value=0),
            "返回结果的最大数量必须是非负整数"
        )
        
        # 检查数据库隔离限制
        if database_checker:
            query_to_check = "SHOW DATABASES"
            is_allowed, violations = database_checker.check_query(query_to_check)
            if not is_allowed:
                violation_details = "; ".join(violations)
                raise SecurityError(f"数据库隔离限制: {violation_details}")
        
        # 构建基础查询
        query = "SHOW DATABASES"
        
        # 执行查询 - 使用异步上下文管理器，不要求预先指定数据库
        async with get_db_connection(require_database=False) as connection:
            # 先获取所有数据库
            results = await execute_query(connection, query)
            
            # 通常结果中每个数据库名会在"Database"字段
            db_field = next((k for k in results[0].keys() if k.lower() == 'database'), None) if results else None
            
            if not db_field:
                logger.warning("查询结果未找到数据库名称字段")
                return MetadataToolBase.format_results(results, operation_type="数据库列表查询")
            
            # 对结果进行过滤
            filtered_results = []
            system_dbs = ['information_schema', 'mysql', 'performance_schema', 'sys']
            
            for item in results:
                db_name = item[db_field]
                
                # 排除系统数据库
                if exclude_system and db_name.lower() in system_dbs:
                    continue
                    
                # 根据模式过滤
                if pattern:
                    pattern_regex = pattern.replace('%', '.*').replace('_', '.')
                    if not re.search(pattern_regex, db_name, re.IGNORECASE):
                        continue
                        
                filtered_results.append(item)
            
            # 限制返回数量
            if limit > 0 and len(filtered_results) > limit:
                filtered_results = filtered_results[:limit]
                logger.debug(f"结果数量已限制为前{limit}个")
            
            # 返回结果
            metadata_info = {
                "metadata_info": {
                    "operation_type": "数据库列表查询",
                    "result_count": len(filtered_results),
                    "total_count": len(results),
                    "filtered": {
                        "pattern": pattern,
                        "exclude_system": exclude_system,
                        "limited": len(filtered_results) < len(results)
                    }
                },
                "results": filtered_results
            }
            
            return json.dumps(metadata_info, default=str)
    
    @mcp.tool()
    @MetadataToolBase.handle_query_error
    async def mysql_show_variables(pattern: Optional[str] = None, global_scope: bool = False) -> str:
        """
        获取MySQL系统变量
        
        Args:
            pattern: 变量名称匹配模式 (可选, 例如 '%buffer%')
            global_scope: 是否查询全局变量 (默认为会话变量)
            
        Returns:
            系统变量的JSON字符串
        """
        # 获取当前环境类型
        from src.security.sql_analyzer import sql_analyzer
        env_type = sql_analyzer.env_type
        
        # 检查环境权限
        if not check_environment_permission(env_type, 'variables'):
            raise SecurityError("当前环境不允许查询系统变量")
            
        # 参数验证
        if pattern:
            MetadataToolBase.validate_parameter(
                "pattern", pattern,
                SQLValidators.validate_like_pattern,
                "变量模式只能包含字母、数字、下划线和通配符(%_)"
            )
            
        # 构建查询
        scope = "GLOBAL" if global_scope else "SESSION"
        query = f"SHOW {scope} VARIABLES"
        if pattern:
            query += f" LIKE '{pattern}'"
            
        logger.debug(f"执行查询: {query}")
        
        async with get_db_connection() as connection:
            results = await execute_query(connection, query)
            
            # 生产环境中过滤敏感信息
            if env_type == EnvironmentType.PRODUCTION:
                results = filter_sensitive_info(results)
                
            return MetadataToolBase.format_results(results, operation_type="系统变量查询")
    
    @mcp.tool()
    @MetadataToolBase.handle_query_error
    async def mysql_show_status(pattern: Optional[str] = None, global_scope: bool = False) -> str:
        """
        获取MySQL服务器状态
        
        Args:
            pattern: 状态名称匹配模式 (可选, 例如 '%conn%')
            global_scope: 是否查询全局状态 (默认为会话状态)
            
        Returns:
            服务器状态的JSON字符串
        """
        # 获取当前环境类型
        from src.security.sql_analyzer import sql_analyzer
        env_type = sql_analyzer.env_type
        
        # 检查环境权限
        if not check_environment_permission(env_type, 'status'):
            raise SecurityError("当前环境不允许查询系统状态")
            
        # 参数验证
        if pattern:
            MetadataToolBase.validate_parameter(
                "pattern", pattern,
                SQLValidators.validate_like_pattern,
                "状态模式只能包含字母、数字、下划线和通配符(%_)"
            )
            
        # 构建查询
        scope = "GLOBAL" if global_scope else "SESSION"
        query = f"SHOW {scope} STATUS"
        if pattern:
            query += f" LIKE '{pattern}'"
            
        logger.debug(f"执行查询: {query}")
        
        async with get_db_connection() as connection:
            results = await execute_query(connection, query)
            
            # 生产环境中过滤敏感信息
            if env_type == EnvironmentType.PRODUCTION:
                results = filter_sensitive_info(results)
                
            return MetadataToolBase.format_results(results, operation_type="服务器状态查询") 