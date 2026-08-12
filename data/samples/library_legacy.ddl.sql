-- Test input: SOURCE side, MySQL DDL format.
-- Pair this with library_platform.mongo.json as the destination.
--
-- A library domain, deliberately unrelated to the HR schemas, so a run here
-- proves nothing is hard-coded. It carries the same awkward legacy habits the
-- real assignment schema has: truncated column names, single-char status codes
-- explained only in a comment, TINYINT(1) booleans, split amount + currency
-- columns, and a foreign key that has to become an ObjectId reference.

CREATE DATABASE legacy_library;
USE legacy_library;

CREATE TABLE brnch (
  brnch_id    INT AUTO_INCREMENT PRIMARY KEY,
  brnch_nm    VARCHAR(120) NOT NULL COMMENT 'Branch name',
  addr_l1     VARCHAR(180)          COMMENT 'Street address line 1',
  city_nm     VARCHAR(80),
  st_cd       CHAR(2)               COMMENT 'US state code',
  zip_cd      VARCHAR(10)           COMMENT 'Postal code',
  ctry_cd     CHAR(2)               COMMENT 'ISO 3166-1 alpha-2 country code',
  is_open     TINYINT(1) NOT NULL DEFAULT 1 COMMENT '1 = currently open to the public, 0 = closed'
);

CREATE TABLE bk_master (
  bk_id       INT AUTO_INCREMENT PRIMARY KEY,
  isbn_cd     VARCHAR(20)  NOT NULL UNIQUE COMMENT 'ISBN-13, unique per title',
  ttl         VARCHAR(250) NOT NULL COMMENT 'Book title',
  auth_nm     VARCHAR(160)          COMMENT 'Primary author full name',
  pub_yr      SMALLINT              COMMENT '4-digit year of publication',
  cat_cd      CHAR(1)      NOT NULL COMMENT 'F=Fiction, N=NonFiction, R=Reference',
  copies_tot  INT          NOT NULL DEFAULT 0 COMMENT 'Total copies owned',
  copies_avl  INT          NOT NULL DEFAULT 0 COMMENT 'Copies currently on the shelf',
  is_ref_only TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1 = reference only, cannot be borrowed',
  price_amt   DECIMAL(8,2)          COMMENT 'Acquisition price',
  cur_cd      CHAR(3)               COMMENT 'ISO 4217 currency of price_amt',
  added_dt    DATETIME     NOT NULL COMMENT 'When the title was added to the catalog',
  brnch_id    INT                   COMMENT 'Owning branch',
  CONSTRAINT fk_bk_brnch FOREIGN KEY (brnch_id) REFERENCES brnch(brnch_id)
);

CREATE TABLE mbr_info (
  mbr_id      INT AUTO_INCREMENT PRIMARY KEY,
  mbr_cd      VARCHAR(20) NOT NULL UNIQUE COMMENT 'Human-readable membership code',
  f_nm        VARCHAR(60) NOT NULL COMMENT 'First name',
  l_nm        VARCHAR(60) NOT NULL COMMENT 'Last name',
  eml         VARCHAR(140)         COMMENT 'Email address',
  ph_no       VARCHAR(30)          COMMENT 'Phone number',
  join_dt     DATE        NOT NULL COMMENT 'Date the membership started',
  mbr_stat    CHAR(1)     NOT NULL COMMENT 'A=Active, S=Suspended, X=Expired',
  fine_bal    DECIMAL(7,2) NOT NULL DEFAULT 0.00 COMMENT 'Outstanding fines owed',
  home_brnch  INT                  COMMENT 'Home branch',
  CONSTRAINT fk_mbr_brnch FOREIGN KEY (home_brnch) REFERENCES brnch(brnch_id)
);
