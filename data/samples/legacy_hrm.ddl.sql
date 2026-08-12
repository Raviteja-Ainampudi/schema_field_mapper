-- Same source schema as data/schemas/legacy_hrm.mysql.json, expressed as MySQL DDL.
-- Use this to exercise the DDL parser:
--   python -m schema_mapper.cli --source data/samples/legacy_hrm.ddl.sql --offline
-- Normalizing this file must yield the identical intermediate representation
-- as the JSON form (asserted by tests/test_normalize.py).

CREATE TABLE `emp_master` (
  `emp_id`        INT            NOT NULL AUTO_INCREMENT,
  `emp_cd`        VARCHAR(20)    NOT NULL UNIQUE COMMENT 'human-readable employee code',
  `f_name`        VARCHAR(50)    NOT NULL,
  `l_name`        VARCHAR(50)    NOT NULL,
  `dob`           DATE           NULL,
  `hire_dt`       DATETIME       NULL,
  `term_dt`       DATETIME       NULL COMMENT 'null if still active',
  `dept_id`       INT            NULL,
  `mgr_emp_id`    INT            NULL,
  `job_lvl_cd`    VARCHAR(10)    NULL COMMENT 'e.g. L1, L2, IC3, M1',
  `base_sal`      DECIMAL(12,2)  NULL,
  `sal_currency`  CHAR(3)        NULL COMMENT 'ISO 4217, e.g. USD',
  `work_email`    VARCHAR(120)   NULL UNIQUE,
  `work_phone`    VARCHAR(20)    NULL,
  `office_loc_id` INT            NULL,
  `is_remote`     TINYINT(1)     NULL COMMENT '0 or 1',
  `rec_stat`      CHAR(1)        NULL COMMENT 'A=Active, I=Inactive, T=Terminated',
  `created_ts`    DATETIME       NULL COMMENT 'record creation timestamp',
  `updated_ts`    DATETIME       NULL COMMENT 'last update timestamp',
  PRIMARY KEY (`emp_id`),
  FOREIGN KEY (`dept_id`) REFERENCES `dept_info` (`dept_id`),
  FOREIGN KEY (`mgr_emp_id`) REFERENCES `emp_master` (`emp_id`),
  FOREIGN KEY (`office_loc_id`) REFERENCES `locations` (`loc_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `dept_info` (
  `dept_id`        INT           NOT NULL AUTO_INCREMENT,
  `dept_cd`        VARCHAR(20)   NULL UNIQUE,
  `dept_nm`        VARCHAR(100)  NULL,
  `parent_dept_id` INT           NULL COMMENT 'self-referencing',
  `dept_head_id`   INT           NULL,
  `cost_ctr_cd`    VARCHAR(20)   NULL COMMENT 'finance cost center code',
  `dept_stat`      CHAR(1)       NULL COMMENT 'A=Active, I=Inactive',
  PRIMARY KEY (`dept_id`),
  FOREIGN KEY (`parent_dept_id`) REFERENCES `dept_info` (`dept_id`),
  FOREIGN KEY (`dept_head_id`) REFERENCES `emp_master` (`emp_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `locations` (
  `loc_id`     INT           NOT NULL AUTO_INCREMENT,
  `loc_cd`     VARCHAR(20)   NULL UNIQUE,
  `loc_nm`     VARCHAR(100)  NULL,
  `city`       VARCHAR(80)   NULL,
  `state_prov` VARCHAR(80)   NULL,
  `country_cd` CHAR(2)       NULL COMMENT 'ISO 3166-1 alpha-2',
  `postal_cd`  VARCHAR(20)   NULL,
  `tz_cd`      VARCHAR(50)   NULL COMMENT 'IANA timezone',
  PRIMARY KEY (`loc_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
