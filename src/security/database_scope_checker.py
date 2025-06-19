"""
数据库范围检查器
用于检测和限制SQL查询中的跨数据库访问
"""
import re
import logging
from typing import Set, Optional, List, Tuple
from enum import Enum

logger = logging.getLogger("mysql_server")

class DatabaseAccessLevel(Enum):
    """数据库访问级别"""
    STRICT = "strict"           # 严格模式：只能访问指定数据库
    RESTRICTED = "restricted"   # 限制模式：允许访问指定数据库和系统库
    PERMISSIVE = "permissive"   # 宽松模式：允许访问所有数据库（默认）

class DatabaseScopeViolation(Exception):
    """数据库范围违规异常"""
    pass

class DatabaseScopeChecker:
    """数据库范围检查器"""
    
    # 系统数据库列表
    SYSTEM_DATABASES = {
        'information_schema',
        'mysql', 
        'performance_schema',
        'sys'
    }
    
    # 跨数据库查询模式
    CROSS_DB_PATTERNS = [
        # database.table 格式
        r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\b',
        # SHOW TABLES FROM database
        r'\bSHOW\s+(?:FULL\s+)?TABLES\s+FROM\s+([a-zA-Z_][a-zA-Z0-9_]*)\b',
        # USE database
        r'\bUSE\s+([a-zA-Z_][a-zA-Z0-9_]*)\b',
        # SELECT ... FROM database.table
        r'\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\b',
        # JOIN database.table
        r'\bJOIN\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\b',
        # INSERT INTO database.table
        r'\bINTO\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\b',
        # UPDATE database.table
        r'\bUPDATE\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\b',
        # DELETE FROM database.table
        r'\bDELETE\s+FROM\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\b',
    ]
    
    def __init__(self, allowed_database: Optional[str] = None, 
                 access_level: DatabaseAccessLevel = DatabaseAccessLevel.PERMISSIVE):
        """
        初始化数据库范围检查器
        
        Args:
            allowed_database: 允许访问的数据库名称
            access_level: 访问级别
        """
        self.allowed_database = allowed_database
        self.access_level = access_level
        self.is_enabled = allowed_database is not None and access_level != DatabaseAccessLevel.PERMISSIVE
        
        logger.debug(f"数据库范围检查器初始化: 允许数据库={allowed_database}, 访问级别={access_level.value}, 启用={self.is_enabled}")
    
    def check_query(self, sql_query: str) -> Tuple[bool, List[str]]:
        """
        检查SQL查询是否违反数据库范围限制
        
        Args:
            sql_query: SQL查询语句
            
        Returns:
            (是否允许, 违规详情列表)
        """
        if not self.is_enabled:
            return True, []
        
        violations = []
        
        # 提取查询中涉及的数据库
        referenced_databases = self._extract_databases(sql_query)
        
        for db_name in referenced_databases:
            if not self._is_database_allowed(db_name):
                violations.append(f"不允许访问数据库: {db_name}")
        
        # 检查特殊查询类型
        special_violations = self._check_special_queries(sql_query)
        violations.extend(special_violations)
        
        is_allowed = len(violations) == 0
        
        if violations:
            logger.warning(f"数据库范围检查失败: {violations}")
        
        return is_allowed, violations
    
    def _extract_databases(self, sql_query: str) -> Set[str]:
        """提取SQL查询中涉及的数据库名称"""
        databases = set()
        
        # 标准化SQL（转换为大写，去除多余空格）
        normalized_sql = re.sub(r'\s+', ' ', sql_query.upper().strip())
        
        for pattern in self.CROSS_DB_PATTERNS:
            matches = re.finditer(pattern, normalized_sql, re.IGNORECASE)
            for match in matches:
                # 第一个捕获组通常是数据库名
                if match.groups():
                    db_name = match.group(1).lower()
                    # 过滤掉非数据库名的匹配（如函数名等）
                    if self._is_valid_database_name(db_name):
                        databases.add(db_name)
        
        return databases
    
    def _is_valid_database_name(self, name: str) -> bool:
        """检查是否是有效的数据库名称"""
        # 数据库名称规则：字母、数字、下划线，不能以数字开头
        return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))
    
    def _is_database_allowed(self, db_name: str) -> bool:
        """检查数据库是否被允许访问"""
        db_name_lower = db_name.lower()
        
        # 检查是否是允许的主数据库
        if self.allowed_database and db_name_lower == self.allowed_database.lower():
            return True
        
        # 根据访问级别决定是否允许系统数据库
        if self.access_level == DatabaseAccessLevel.RESTRICTED:
            if db_name_lower in self.SYSTEM_DATABASES:
                return True
        
        return False
    
    def _check_special_queries(self, sql_query: str) -> List[str]:
        """检查特殊类型的查询"""
        violations = []
        normalized_sql = sql_query.upper().strip()
        
        # 检查SHOW DATABASES查询
        if re.search(r'\bSHOW\s+DATABASES\b', normalized_sql):
            if self.access_level == DatabaseAccessLevel.STRICT:
                violations.append("严格模式下不允许执行 SHOW DATABASES")
        
        # 检查USE语句
        if re.search(r'\bUSE\s+', normalized_sql):
            violations.append("不允许使用 USE 语句切换数据库")
        
        # 检查系统表访问
        system_table_patterns = [
            r'\bmysql\.user\b',
            r'\bmysql\.db\b', 
            r'\binformation_schema\.',
            r'\bperformance_schema\.',
            r'\bsys\.'
        ]
        
        for pattern in system_table_patterns:
            if re.search(pattern, normalized_sql, re.IGNORECASE):
                if self.access_level == DatabaseAccessLevel.STRICT:
                    violations.append(f"严格模式下不允许访问系统表")
                    break
        
        return violations
    
    def get_allowed_databases(self) -> Set[str]:
        """获取允许访问的数据库列表"""
        allowed = set()
        
        if self.allowed_database:
            allowed.add(self.allowed_database.lower())
        
        if self.access_level == DatabaseAccessLevel.RESTRICTED:
            allowed.update(self.SYSTEM_DATABASES)
        
        return allowed
    
    def is_cross_database_query(self, sql_query: str) -> bool:
        """检查是否是跨数据库查询"""
        referenced_dbs = self._extract_databases(sql_query)
        return len(referenced_dbs) > 0

# 便捷函数
def create_database_checker(allowed_database: Optional[str] = None, 
                          access_level: str = "permissive") -> DatabaseScopeChecker:
    """
    创建数据库范围检查器的便捷函数
    
    Args:
        allowed_database: 允许访问的数据库名称
        access_level: 访问级别字符串 (strict/restricted/permissive)
        
    Returns:
        DatabaseScopeChecker实例
    """
    try:
        level = DatabaseAccessLevel(access_level.lower())
    except ValueError:
        logger.warning(f"无效的访问级别: {access_level}，使用默认的 permissive")
        level = DatabaseAccessLevel.PERMISSIVE
    
    return DatabaseScopeChecker(allowed_database, level) 