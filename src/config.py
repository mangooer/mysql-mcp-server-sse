import os
from typing import Set, List
from enum import IntEnum, Enum

# 环境变量
class EnvironmentType(Enum):
    """环境类型"""
    DEVELOPMENT = 'development'
    PRODUCTION = 'production'

class SQLRiskLevel(IntEnum):
    """SQL操作风险等级"""
    LOW = 1      # 查询操作（SELECT）
    MEDIUM = 2   # 基本数据修改（INSERT，有WHERE的UPDATE/DELETE）
    HIGH = 3     # 结构变更（CREATE/ALTER）和无WHERE的数据修改
    CRITICAL = 4 # 危险操作（DROP/TRUNCATE等）

# 服务器配置
class ServerConfig:
    """服务器配置"""
    HOST = os.getenv('HOST', '127.0.0.1')
    PORT = int(os.getenv('PORT', '3000'))
    
# 数据库配置
class DatabaseConfig:
    """数据库连接配置"""
    HOST = os.getenv('MYSQL_HOST', 'localhost')
    USER = os.getenv('MYSQL_USER', 'root')
    PASSWORD = os.getenv('MYSQL_PASSWORD', '')
    DATABASE = os.getenv('MYSQL_DATABASE', '')
    PORT = int(os.getenv('MYSQL_PORT', '3306'))
    CONNECTION_TIMEOUT = int(os.getenv('DB_CONNECTION_TIMEOUT', '5'))
    AUTH_PLUGIN = os.getenv('DB_AUTH_PLUGIN', 'mysql_native_password')
    
    @staticmethod
    def get_config():
        """获取数据库配置字典"""
        return {
            'host': DatabaseConfig.HOST,
            'user': DatabaseConfig.USER,
            'password': DatabaseConfig.PASSWORD,
            'database': DatabaseConfig.DATABASE,
            'port': DatabaseConfig.PORT,
            'connection_timeout': DatabaseConfig.CONNECTION_TIMEOUT,
            'auth_plugin': DatabaseConfig.AUTH_PLUGIN
        }

# 数据库连接池配置
class ConnectionPoolConfig:
    """数据库连接池配置"""
    # 连接池最小连接数
    MIN_SIZE = int(os.getenv('DB_POOL_MIN_SIZE', '5'))
    # 连接池最大连接数
    MAX_SIZE = int(os.getenv('DB_POOL_MAX_SIZE', '20'))
    # 连接池回收时间（秒）
    POOL_RECYCLE = int(os.getenv('DB_POOL_RECYCLE', '300'))
    # 连接最大存活时间（秒，0表示不限制）
    MAX_LIFETIME = int(os.getenv('DB_POOL_MAX_LIFETIME', '0'))
    # 连接获取超时时间（秒）
    ACQUIRE_TIMEOUT = float(os.getenv('DB_POOL_ACQUIRE_TIMEOUT', '10.0'))
    # 是否启用连接池
    ENABLED = os.getenv('DB_POOL_ENABLED', 'true').lower() in ('true', 'yes', '1')
    
    @staticmethod
    def get_config():
        """获取连接池配置字典"""
        return {
            'minsize': ConnectionPoolConfig.MIN_SIZE,
            'maxsize': ConnectionPoolConfig.MAX_SIZE,
            'pool_recycle': ConnectionPoolConfig.POOL_RECYCLE,
            'max_lifetime': ConnectionPoolConfig.MAX_LIFETIME,
            'acquire_timeout': ConnectionPoolConfig.ACQUIRE_TIMEOUT,
            'enabled': ConnectionPoolConfig.ENABLED
        }

# 安全配置
class SecurityConfig:
    """安全相关配置"""
    # 环境类型
    ENV_TYPE_STR = os.getenv('ENV_TYPE', 'development').lower()
    try:
        ENV_TYPE = EnvironmentType(ENV_TYPE_STR)
    except ValueError:
        ENV_TYPE = EnvironmentType.DEVELOPMENT
    
    # 允许的风险等级
    ALLOWED_RISK_LEVELS_STR = os.getenv('ALLOWED_RISK_LEVELS', 'LOW,MEDIUM')
    ALLOWED_RISK_LEVELS = set()
    for level_str in ALLOWED_RISK_LEVELS_STR.upper().split(','):
        level_str = level_str.strip()
        try:
            ALLOWED_RISK_LEVELS.add(SQLRiskLevel[level_str])
        except KeyError:
            pass
    
    # 如果是生产环境且没有明确配置风险等级，则只允许LOW风险操作
    if ENV_TYPE == EnvironmentType.PRODUCTION and not os.getenv('ALLOWED_RISK_LEVELS'):
        ALLOWED_RISK_LEVELS = {SQLRiskLevel.LOW}
    
    # 最大SQL长度
    MAX_SQL_LENGTH = int(os.getenv('MAX_SQL_LENGTH', '1000'))
    
    # 敏感信息查询
    ALLOW_SENSITIVE_INFO = os.getenv('ALLOW_SENSITIVE_INFO', 'false').lower() in ('true', 'yes', '1')
    
    # 阻止的模式
    BLOCKED_PATTERNS_STR = os.getenv('BLOCKED_PATTERNS', '')
    BLOCKED_PATTERNS = [p.strip() for p in BLOCKED_PATTERNS_STR.split(',') if p.strip()]
    
    # 查询检查
    ENABLE_QUERY_CHECK = os.getenv('ENABLE_QUERY_CHECK', 'true').lower() in ('true', 'yes', '1')
    
    # 数据库隔离配置
    ENABLE_DATABASE_ISOLATION = os.getenv('ENABLE_DATABASE_ISOLATION', 'false').lower() in ('true', 'yes', '1')
    DATABASE_ACCESS_LEVEL = os.getenv('DATABASE_ACCESS_LEVEL', 'permissive').lower()
    
    # 生产环境强制数据库隔离
    if ENV_TYPE == EnvironmentType.PRODUCTION and not os.getenv('DATABASE_ACCESS_LEVEL'):
        DATABASE_ACCESS_LEVEL = 'restricted'  # 生产环境默认使用限制模式
        ENABLE_DATABASE_ISOLATION = True
        logger = __import__('logging').getLogger("mysql_server")
        logger.info("生产环境自动启用数据库隔离，访问级别设为 restricted")

# SQL操作配置
class SQLConfig:
    """SQL操作相关配置"""
    # 基础操作集合
    DDL_OPERATIONS = {
        'CREATE', 'ALTER', 'DROP', 'TRUNCATE', 'RENAME'
    }
    
    DML_OPERATIONS = {
        'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'MERGE'
    }
    
    # 元数据操作集合
    METADATA_OPERATIONS = {
        'SHOW', 'DESC', 'DESCRIBE', 'EXPLAIN', 'HELP', 
        'ANALYZE', 'CHECK', 'CHECKSUM', 'OPTIMIZE'
    } 