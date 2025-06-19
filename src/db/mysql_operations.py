import logging
import aiomysql
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Union
import threading
import weakref

from ..config import DatabaseConfig, SecurityConfig, SQLConfig, ConnectionPoolConfig
from ..security.sql_analyzer import SQLOperationType
from ..security.interceptor import SQLInterceptor, SecurityException
from ..security.sql_parser import SQLParser

logger = logging.getLogger("mysql_server")

# 初始化安全组件
sql_analyzer = SQLOperationType()
sql_interceptor = SQLInterceptor(sql_analyzer)

# 全局连接池 - 使用线程本地存储
_pools = threading.local()

# 定期回收无效连接池
_cleanup_interval = 300  # 秒，可根据需要调整
_last_cleanup = 0

def _cleanup_unused_pools():
    """回收无效或已关闭的连接池，释放资源"""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _cleanup_interval:
        return
    _last_cleanup = now
    if hasattr(_pools, 'pools'):
        to_remove = []
        for loop_id, pool in list(_pools.pools.items()):
            # 检查事件循环是否还活着
            if pool.closed:
                to_remove.append(loop_id)
                continue
            # 尝试获取事件循环对象
            for loop in asyncio.all_tasks():
                if id(loop.get_loop()) == loop_id:
                    break
            else:
                # 没找到对应事件循环，关闭池
                pool.close()
                to_remove.append(loop_id)
                logger.info(f"检测到无主事件循环，已关闭连接池 (事件循环ID: {loop_id})")
        for loop_id in to_remove:
            del _pools.pools[loop_id]

def get_db_config():
    """动态获取数据库配置"""
    # 获取基础配置
    config = DatabaseConfig.get_config()
    
    # aiomysql使用不同的配置键名，进行映射
    aiomysql_config = {
        'host': config['host'],
        'user': config['user'],
        'password': config['password'],
        'db': config['database'],  # 'database' -> 'db'
        'port': config['port'],
        'connect_timeout': config.get('connection_timeout', 5),  # 'connection_timeout' -> 'connect_timeout'
        'charset': 'utf8mb4',  # 确保字符集支持
    }
    
    # MySQL 8.0 认证插件支持
    # 如果指定了认证插件且不是默认的mysql_native_password，则添加到配置中
    auth_plugin = config.get('auth_plugin', 'mysql_native_password')
    if auth_plugin != 'mysql_native_password':
        # 对于caching_sha2_password等现代认证插件，需要确保cryptography包可用
        try:
            import cryptography
            # 添加认证插件配置以支持caching_sha2_password
            aiomysql_config.update({
                'auth_plugin': auth_plugin
            })
            logger.debug(f"使用认证插件: {auth_plugin} (已检测到 cryptography 包)")
        except ImportError:
            logger.warning(f"检测到认证插件 {auth_plugin}，但未安装 cryptography 包")
            logger.warning("将回退到 mysql_native_password 认证方式")
            logger.warning("要完全支持 MySQL 8.0 认证，请安装: pip install cryptography")
    else:
        logger.debug(f"使用认证插件: {auth_plugin}")
    
    return aiomysql_config

# 自定义异常类，细化错误处理
class MySQLConnectionError(Exception):
    """数据库连接错误基类"""
    pass

class MySQLAuthError(MySQLConnectionError):
    """认证错误"""
    pass

class MySQLDatabaseNotFoundError(MySQLConnectionError):
    """数据库不存在错误"""
    pass

class MySQLServerError(MySQLConnectionError):
    """服务器连接错误"""
    pass

class MySQLAuthPluginError(MySQLConnectionError):
    """认证插件错误"""
    pass

async def init_db_pool(min_size: Optional[int] = None, max_size: Optional[int] = None, require_database: bool = True):
    """
    初始化数据库连接池
    
    Args:
        min_size: 连接池最小连接数 (可选，默认从配置读取)
        max_size: 连接池最大连接数 (可选，默认从配置读取)
        require_database: 是否要求指定数据库
        
    Returns:
        连接池对象
    
    Raises:
        MySQLConnectionError: 连接池初始化失败时
    """
    try:
        # 获取数据库配置
        db_config = get_db_config()
        
        # 检查是否需要数据库名
        if require_database and not db_config.get('db'):
            raise MySQLDatabaseNotFoundError("数据库名称未设置，请检查环境变量MYSQL_DATABASE")
            
        # 如果不需要指定数据库，且db为空，则移除db参数
        if not require_database and not db_config.get('db'):
            db_config.pop('db', None)
        
        # 获取当前事件循环
        current_loop = asyncio.get_event_loop()
        loop_id = id(current_loop)
        
        # 获取连接池配置
        pool_config = ConnectionPoolConfig.get_config()
        
        # 使用传入的参数或者配置值
        min_size = min_size if min_size is not None else pool_config['minsize']
        max_size = max_size if max_size is not None else pool_config['maxsize']
        pool_recycle = pool_config['pool_recycle']
        
        # 检查是否启用连接池
        if not pool_config['enabled']:
            logger.warning("连接池功能已被禁用，使用直接连接")
            # 创建单连接的池
            min_size = 1
            max_size = 1
        
        # 创建连接池
        logger.info(f"初始化连接池: 最小连接数={min_size}, 最大连接数={max_size}, 回收时间={pool_recycle}秒")
        pool = await aiomysql.create_pool(
            minsize=min_size,
            maxsize=max_size,
            pool_recycle=pool_recycle,
            echo=False,  # 不记录SQL执行日志，由我们自己的日志系统处理
            loop=current_loop,  # 显式指定事件循环
            **db_config
        )
        
        # 将池存储在线程本地存储中，键是事件循环ID
        if not hasattr(_pools, 'pools'):
            _pools.pools = {}
        _pools.pools[loop_id] = pool
        
        # 注册事件循环关闭时自动清理
        def _finalizer(p=pool, lid=loop_id):
            if not p.closed:
                p.close()
                logger.info(f"事件循环关闭时自动关闭连接池 (事件循环ID: {lid})")
        try:
            weakref.finalize(current_loop, _finalizer)
        except Exception as e:
            logger.warning(f"注册事件循环关闭回调失败: {e}")
        
        logger.info(f"MySQL连接池初始化成功，最小连接数: {min_size}，最大连接数: {max_size}，事件循环ID: {loop_id}")
        return pool
    except aiomysql.Error as err:
        error_msg = str(err)
        logger.error(f"数据库连接池初始化失败: {error_msg}")
        
        # 细化错误类型
        if "Access denied" in error_msg:
            raise MySQLAuthError("访问被拒绝，请检查用户名和密码")
        elif "Unknown database" in error_msg:
            raise MySQLDatabaseNotFoundError(f"数据库'{db_config.get('db', '')}'不存在")
        elif "Can't connect" in error_msg or "Connection refused" in error_msg:
            raise MySQLServerError("无法连接到MySQL服务器，请检查服务是否启动")
        elif "Authentication plugin" in error_msg:
            current_auth = DatabaseConfig.AUTH_PLUGIN
            error_detail = f"认证插件问题: {error_msg}"
            if current_auth == 'caching_sha2_password':
                error_detail += "\n解决方案："
                error_detail += "\n1. 确保已安装 cryptography 包: pip install cryptography"
                error_detail += "\n2. 或者修改用户认证方式为 mysql_native_password"
                error_detail += "\n3. 或者在 .env 中设置 DB_AUTH_PLUGIN=mysql_native_password"
            else:
                error_detail += f"\n当前认证插件配置: {current_auth}"
                error_detail += "\n请检查 MySQL 用户的认证插件设置是否匹配"
            raise MySQLAuthPluginError(error_detail)
        else:
            raise MySQLConnectionError(f"数据库连接失败: {error_msg}")
    except Exception as e:
        logger.error(f"连接池初始化发生未预期错误: {str(e)}")
        raise MySQLConnectionError(f"连接池初始化失败: {str(e)}")

def get_pool_for_current_loop():
    """获取当前事件循环对应的连接池"""
    _cleanup_unused_pools()  # 每次获取时尝试回收
    try:
        # 获取当前事件循环ID
        current_loop = asyncio.get_event_loop()
        loop_id = id(current_loop)
        
        # 检查是否有此循环的连接池
        if hasattr(_pools, 'pools') and loop_id in _pools.pools:
            pool = _pools.pools[loop_id]
            # 检查连接池是否已关闭
            if pool.closed:
                logger.debug(f"连接池已关闭，将重新创建 (事件循环ID: {loop_id})")
                return None
            return pool
        return None
    except Exception as e:
        logger.error(f"获取当前事件循环的连接池失败: {str(e)}")
        return None

@asynccontextmanager
async def get_db_connection(require_database: bool = True):
    """
    从连接池获取数据库连接的异步上下文管理器
    
    Args:
        require_database: 是否要求必须指定数据库。设置为False时可以执行如SHOW DATABASES等不需要
                         指定具体数据库的操作。
    
    Yields:
        aiomysql.Connection: 数据库连接对象
    """
    # 获取当前事件循环的连接池
    pool = get_pool_for_current_loop()
    
    # 如果没有连接池，则初始化一个
    if pool is None:
        pool = await init_db_pool(require_database=require_database)
    
    try:
        # 从连接池获取连接
        async with pool.acquire() as connection:
            yield connection
    except aiomysql.Error as err:
        error_msg = str(err)
        logger.error(f"获取数据库连接失败: {error_msg}")
        
        if "Access denied" in error_msg:
            raise MySQLAuthError("访问被拒绝，请检查用户名和密码")
        elif "Unknown database" in error_msg:
            db_config = get_db_config()
            raise MySQLDatabaseNotFoundError(f"数据库'{db_config.get('db', '')}'不存在")
        elif "Can't connect" in error_msg or "Connection refused" in error_msg:
            raise MySQLServerError("无法连接到MySQL服务器，请检查服务是否启动")
        elif "Authentication plugin" in error_msg:
            current_auth = DatabaseConfig.AUTH_PLUGIN
            error_detail = f"认证插件问题: {error_msg}"
            if current_auth == 'caching_sha2_password':
                error_detail += "\n解决方案："
                error_detail += "\n1. 确保已安装 cryptography 包: pip install cryptography"
                error_detail += "\n2. 或者修改用户认证方式为 mysql_native_password"
                error_detail += "\n3. 或者在 .env 中设置 DB_AUTH_PLUGIN=mysql_native_password"
            else:
                error_detail += f"\n当前认证插件配置: {current_auth}"
                error_detail += "\n请检查 MySQL 用户的认证插件设置是否匹配"
            raise MySQLAuthPluginError(error_detail)
        else:
            raise MySQLConnectionError(f"数据库连接失败: {error_msg}")
    except Exception as e:
        logger.error(f"获取数据库连接时发生未预期错误: {str(e)}")
        raise MySQLConnectionError(f"获取数据库连接失败: {str(e)}")

async def close_all_pools():
    """关闭所有连接池"""
    if hasattr(_pools, 'pools'):
        for loop_id, pool in list(_pools.pools.items()):
            if not pool.closed:
                pool.close()
                await pool.wait_closed()
                logger.info(f"连接池已关闭 (事件循环ID: {loop_id})")
        _pools.pools = {}

@asynccontextmanager
async def transaction(connection):
    """
    事务上下文管理器
    
    用法示例:
    async with get_db_connection() as conn:
        async with transaction(conn):
            await execute_query(conn, "INSERT INTO...")
            await execute_query(conn, "UPDATE...")
    
    Args:
        connection: 数据库连接
        
    Yields:
        connection: 事务中的数据库连接
    """
    try:
        # 开始事务
        await connection.begin()
        logger.debug("事务已开始")
        yield connection
        # 提交事务
        await connection.commit()
        logger.debug("事务已提交")
    except Exception as e:
        # 回滚事务
        await connection.rollback()
        logger.error(f"事务执行失败，已回滚: {str(e)}")
        raise

def normalize_result(result_rows):
    """
    将 DictRow 对象转换为普通字典
    
    Args:
        result_rows: 查询结果行列表
        
    Returns:
        包含普通字典的列表
    """
    if not result_rows:
        return []
        
    return [dict(row) for row in result_rows]

async def execute_query(connection, query: str, params: Optional[Dict[str, Any]] = None, 
                   batch_size: int = 1000, stream_results: bool = False) -> List[Dict[str, Any]]:
    """
    在给定的数据库连接上执行查询
    
    Args:
        connection: 数据库连接
        query: SQL查询语句
        params: 查询参数 (可选)
        batch_size: 批处理大小，控制每次从游标获取的记录数量 (仅当stream_results=True时有效)
        stream_results: 是否使用流式处理获取大型结果集
        
    Returns:
        查询结果列表，如果是修改操作则返回影响的行数
        
    Raises:
        SecurityException: 当操作被安全机制拒绝时
        ValueError: 当查询执行失败时
    """
    cursor = None
    parsed_sql = None  # 初始化SQL解析结果
    start_time = time.time()  # 记录查询开始时间
    
    try:
        # 安全检查
        if not await sql_interceptor.check_operation(query):
            raise SecurityException("操作被安全机制拒绝")
            
        # 创建异步游标，支持字典结果
        cursor = await connection.cursor(aiomysql.DictCursor)
        
        # 执行查询 - 异步执行
        if params:
            # 检查参数类型并转换为适合aiomysql的格式
            if isinstance(params, dict):
                # 构建使用%(key)s格式的查询
                await cursor.execute(query, params)
            else:
                await cursor.execute(query, params)
        else:
            await cursor.execute(query)
        
        # 解析SQL语句获取操作类型
        parsed_sql = SQLParser.parse_query(query)
        operation = parsed_sql['operation_type']
        
        # 对于修改操作，提交事务并返回影响的行数
        if parsed_sql['category'] == 'DML' and operation in {'UPDATE', 'DELETE', 'INSERT'}:
            affected_rows = cursor.rowcount
            # 提交事务，确保更改被保存
            await connection.commit()
            logger.debug(f"修改操作 {operation} 影响了 {affected_rows} 行数据")
            
            # 记录查询执行时间
            execution_time = time.time() - start_time
            _log_query_performance(query, execution_time, operation)
            
            return [{'affected_rows': affected_rows}]
        
        # 处理元数据查询操作
        if parsed_sql['category'] == 'METADATA':
            # 元数据查询通常结果较小，直接获取所有结果
            results = await cursor.fetchall()
            
            # 没有结果时返回空列表但添加元信息
            if not results:
                logger.debug(f"元数据查询 {operation} 没有返回结果")
                # 记录查询执行时间
                execution_time = time.time() - start_time
                _log_query_performance(query, execution_time, operation)
                return [{'metadata_operation': operation, 'result_count': 0}]
                
            # 优化结果格式 - 为元数据结果添加额外信息
            metadata_results = []
            for row in results:
                # 将行结果转为普通字典，而不是DictCursor的特殊对象
                row_dict = dict(row)
                
                # 对某些特定元数据查询进行特殊处理
                if operation == 'SHOW' and 'Table' in row_dict:
                    # SHOW TABLES 结果增强
                    row_dict['table_name'] = row_dict['Table']
                elif operation in {'DESC', 'DESCRIBE'} and 'Field' in row_dict:
                    # DESC/DESCRIBE 表结构结果增强
                    row_dict['column_name'] = row_dict['Field']
                    row_dict['data_type'] = row_dict['Type']
                
                metadata_results.append(row_dict)
                
            logger.debug(f"元数据查询 {operation} 返回 {len(metadata_results)} 条结果")
            
            # 记录查询执行时间
            execution_time = time.time() - start_time
            _log_query_performance(query, execution_time, operation)
            
            return metadata_results
        
        # 对于普通查询操作，根据stream_results参数决定结果获取方式
        if stream_results:
            # 流式处理大型结果集 - 分批获取
            all_results = []
            total_fetched = 0
            
            # 分批次获取结果
            while True:
                batch = await cursor.fetchmany(batch_size)
                if not batch:
                    break
                    
                # 使用工具函数将DictRow对象转换为普通字典
                dict_batch = normalize_result(batch)
                all_results.extend(dict_batch)
                
                total_fetched += len(batch)
                logger.debug(f"已获取 {total_fetched} 条记录")
                
                # 检查是否还有剩余结果
                if len(batch) < batch_size:
                    break
                    
            logger.debug(f"流式查询总共返回 {len(all_results)} 条结果")
            
            # 记录查询执行时间
            execution_time = time.time() - start_time
            _log_query_performance(query, execution_time, operation)
            
            return all_results
        else:
            # 传统方式 - 一次性获取所有结果
            results = await cursor.fetchall()
            
            # 使用工具函数将DictRow对象转换为普通字典
            dict_results = normalize_result(results)
            
            logger.debug(f"查询返回 {len(dict_results)} 条结果")
            
            # 记录查询执行时间
            execution_time = time.time() - start_time
            _log_query_performance(query, execution_time, operation)
            
            return dict_results
            
    except SecurityException as security_err:
        logger.error(f"安全检查失败: {str(security_err)}")
        raise
    except aiomysql.Error as query_err:
        # 如果发生错误，进行回滚
        if parsed_sql and parsed_sql['operation_type'] in {'UPDATE', 'DELETE', 'INSERT'}:
            try:
                await connection.rollback()
                logger.debug("事务已回滚")
            except Exception as rollback_err:
                logger.error(f"回滚事务失败: {str(rollback_err)}")
        logger.error(f"查询执行失败: {str(query_err)}")
        raise ValueError(f"查询执行失败: {str(query_err)}")
    finally:
        # 确保游标正确关闭
        if cursor:
            await cursor.close()
            logger.debug("数据库游标已关闭")

def _log_query_performance(query: str, execution_time: float, operation_type: str = ""):
    """
    记录查询性能日志
    
    Args:
        query: SQL查询语句
        execution_time: 执行时间（秒）
        operation_type: 操作类型
    """
    # 截断长查询以避免日志过大
    truncated_query = query[:150] + '...' if len(query) > 150 else query
    
    # 根据执行时间确定日志级别
    if execution_time >= 1.0:  # 超过1秒的查询记录为警告
        logger.warning(f"慢查询 [{operation_type}]: {truncated_query} 执行时间: {execution_time:.4f}秒")
    elif execution_time >= 0.5:  # 超过0.5秒的查询记录为提醒
        logger.info(f"较慢查询 [{operation_type}]: {truncated_query} 执行时间: {execution_time:.4f}秒")
    else:
        logger.debug(f"查询 [{operation_type}] 执行时间: {execution_time:.4f}秒")

async def execute_transaction_queries(connection, queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    在单个事务中执行多个查询
    
    Args:
        connection: 数据库连接
        queries: 查询列表，每个查询是一个包含 'query' 和可选 'params' 的字典
        
    Returns:
        所有查询的结果列表
        
    Raises:
        Exception: 当任何查询执行失败时，整个事务将回滚
    """
    results = []
    
    async with transaction(connection):
        for query_item in queries:
            query = query_item['query']
            params = query_item.get('params')
            
            # 执行单个查询
            result = await execute_query(connection, query, params)
            results.append(result)
            
    return results

async def get_current_database() -> str:
    """
    获取当前连接的数据库名称
    
    Returns:
        当前数据库名称，如果未设置则返回空字符串
    """
    async with get_db_connection(require_database=False) as connection:
        try:
            cursor = await connection.cursor(aiomysql.DictCursor)
            await cursor.execute("SELECT DATABASE() as db")
            result = await cursor.fetchone()
            await cursor.close()
            
            if result and 'db' in result:
                return result['db'] or ""
            return ""
        except Exception as e:
            logger.error(f"获取当前数据库名称失败: {str(e)}")
            return ""