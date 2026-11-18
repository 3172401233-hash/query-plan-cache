import unittest

from plancache import (
    Analyze,
    BinaryOp,
    Column,
    Hint,
    HIT,
    InList,
    Literal,
    MISS,
    Parameter,
    PlanCache,
    REJECT_HINT,
    REJECT_PARAMETER_SHAPE,
    REJECT_STALE_SCHEMA,
    REJECT_STALE_STATS,
    RenameColumn,
    SchemaCatalog,
    Select,
    Table,
    analyze,
    in_list_bucket,
)
from plancache.params import ParameterSummary


def select_orders(where, hints=()):
    return Select(
        columns=(Column("id", "o"), Column("amount", "o")),
        from_=(Table("orders", "o"),),
        where=where,
        hints=hints,
    )


def amount_eq_literal(value):
    return select_orders(BinaryOp("=", Column("amount", "o"), Literal(value)))


def amount_eq_param():
    return select_orders(BinaryOp("=", Column("amount", "o"), Parameter(1)))


def in_query(length):
    return select_orders(
        InList(
            Column("id", "o"),
            tuple(Literal(i) for i in range(length)),
        )
    )


class FingerprintTest(unittest.TestCase):
    def test_literals_are_placeholders(self):
        first = analyze(amount_eq_literal(10))
        second = analyze(amount_eq_literal(999))
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertIn("?", first.canonical)
        self.assertNotIn("999", second.canonical)

    def test_alias_renames_share_fingerprint(self):
        original = Select(
            columns=(Column("id", "o"),),
            from_=(Table("orders", "o"),),
            where=BinaryOp("=", Column("id", "o"), Literal(1)),
        )
        renamed_alias = Select(
            columns=(Column("id", "ord"),),
            from_=(Table("orders", "ord"),),
            where=BinaryOp("=", Column("id", "ord"), Literal(2)),
        )
        self.assertEqual(
            analyze(original).fingerprint, analyze(renamed_alias).fingerprint
        )
        self.assertIn("T0", analyze(original).canonical)

    def test_different_structure_different_fingerprint(self):
        eq_query = amount_eq_literal(1)
        gt_query = select_orders(BinaryOp(">", Column("amount", "o"), Literal(1)))
        self.assertNotEqual(analyze(eq_query).fingerprint, analyze(gt_query).fingerprint)

    def test_dependencies_extracted(self):
        analysis = analyze(amount_eq_literal(1))
        self.assertEqual(analysis.tables, frozenset({"orders"}))
        self.assertEqual(analysis.columns, frozenset({("orders", "id"), ("orders", "amount")}))

    def test_in_list_buckets(self):
        self.assertEqual(in_list_bucket(1), "1-1")
        self.assertEqual(in_list_bucket(3), "2-5")
        self.assertEqual(in_list_bucket(25), "6-25")
        self.assertEqual(in_list_bucket(26), "26-100")
        self.assertEqual(in_list_bucket(300), ">100")


class LiteralReuseTest(unittest.TestCase):
    def setUp(self):
        self.cache = PlanCache(SchemaCatalog())

    def test_same_structure_hits_across_literals(self):
        first = self.cache.lookup(amount_eq_literal(10))
        self.assertEqual(first.reason, MISS)
        self.cache.put(amount_eq_literal(10), plan="index_scan", cost=12.0)

        second = self.cache.lookup(amount_eq_literal(42))
        self.assertTrue(second.hit)
        self.assertEqual(second.reason, HIT)
        self.assertEqual(second.plan, "index_scan")
        self.assertEqual(second.evicted, set())

    def test_parametrized_query_hits(self):
        query = amount_eq_param()
        self.cache.put(query, "param_plan", 5.0)
        result = self.cache.lookup(
            query, ParameterSummary(values={1: 100})
        )
        self.assertTrue(result.hit)


class InListTest(unittest.TestCase):
    def setUp(self):
        self.cache = PlanCache(SchemaCatalog())

    def test_same_bucket_reuses_plan(self):
        self.cache.put(in_query(3), "in_small", 10.0)
        result = self.cache.lookup(in_query(4))
        self.assertTrue(result.hit)
        self.assertEqual(result.plan, "in_small")

    def test_extreme_length_gets_different_key(self):
        self.cache.put(in_query(3), "in_small", 10.0)
        result = self.cache.lookup(in_query(300))
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, REJECT_PARAMETER_SHAPE)
        self.assertEqual(result.evicted, set())

        self.cache.put(in_query(300), "in_huge", 900.0)
        self.assertEqual(len(self.cache), 2)
        self.assertTrue(self.cache.lookup(in_query(3)).hit)
        self.assertTrue(self.cache.lookup(in_query(300)).hit)


class SchemaInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.catalog = SchemaCatalog()
        self.cache = PlanCache(self.catalog)
        self.cache.put(amount_eq_literal(1), "orders_plan", 10.0)
        # A plan on an unrelated table must survive orders-table DDL.
        self.cache.put(
            Select(
                columns=(Column("x", "c"),),
                from_=(Table("customers", "c"),),
                where=BinaryOp("=", Column("x", "c"), Literal(1)),
            ),
            "customers_plan",
            3.0,
        )

    def test_rename_column_only_evicts_dependent_plan(self):
        evicted = self.cache.apply_event(RenameColumn("orders", "amount", "total"))
        self.assertEqual(len(evicted), 1)

        result = self.cache.lookup(amount_eq_literal(2))
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, MISS)
        self.assertEqual(len(result.evicted), 0)

        customers = self.cache.lookup(
            Select(
                columns=(Column("x", "c"),),
                from_=(Table("customers", "c"),),
                where=BinaryOp("=", Column("x", "c"), Literal(9)),
            )
        )
        self.assertTrue(customers.hit)
        self.assertEqual(customers.plan, "customers_plan")

    def test_lookup_reports_evicted_stale_entry(self):
        self.catalog.rename_column("orders", "amount", "total")
        result = self.cache.lookup(amount_eq_literal(5))
        self.assertEqual(result.reason, REJECT_STALE_SCHEMA)
        self.assertEqual(len(result.evicted), 1)


class StatsInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.catalog = SchemaCatalog()
        self.cache = PlanCache(self.catalog)
        self.query = select_orders(
            BinaryOp("=", Column("amount", "o"), Parameter(1))
        )
        self.cache.put(self.query, "stats_plan", 8.0)

    def test_column_stats_refresh_rejects_and_evicts(self):
        evicted = self.cache.apply_event(Analyze("orders", ("amount",)))
        self.assertEqual(len(evicted), 1)
        result = self.cache.lookup(self.query, ParameterSummary(values={1: 5}))
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, MISS)

    def test_lazy_stats_staleness_reported_on_lookup(self):
        self.catalog.analyze("orders", ("amount",))
        result = self.cache.lookup(self.query, ParameterSummary(values={1: 5}))
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, REJECT_STALE_STATS)
        self.assertEqual(len(result.evicted), 1)

    def test_unrelated_column_stats_keeps_plan(self):
        evicted = self.cache.apply_event(Analyze("orders", ("created_at",)))
        self.assertEqual(evicted, set())
        result = self.cache.lookup(self.query, ParameterSummary(values={1: 5}))
        self.assertTrue(result.hit)

    def test_whole_table_analyze_evicts_all_table_plans(self):
        evicted = self.cache.apply_event(Analyze("orders"))
        self.assertEqual(len(evicted), 1)
        self.assertFalse(self.cache.lookup(self.query).hit)


class HintTest(unittest.TestCase):
    def setUp(self):
        self.cache = PlanCache(SchemaCatalog())
        self.query = amount_eq_param()

    def test_different_hints_do_not_share_plan(self):
        hinted = select_orders(
            BinaryOp("=", Column("amount", "o"), Parameter(1)),
            hints=(Hint("INDEX", ("o", "idx_amount")),),
        )
        self.cache.put(hinted, "forced_index", 2.0)

        result = self.cache.lookup(self.query, ParameterSummary(values={1: 1}))
        self.assertFalse(result.hit)
        self.assertEqual(result.reason, REJECT_HINT)

        hint_hit = self.cache.lookup(hinted, ParameterSummary(values={1: 1}))
        self.assertTrue(hint_hit)


class ParameterSensitivityTest(unittest.TestCase):
    def setUp(self):
        self.cache = PlanCache(SchemaCatalog())
        self.query = amount_eq_param()

    def test_null_parameter_is_sensitive(self):
        self.cache.put(
            self.query,
            "non_null_plan",
            4.0,
            ParameterSummary(values={1: 10}),
        )
        null_result = self.cache.lookup(
            self.query, ParameterSummary(values={1: None})
        )
        self.assertFalse(null_result.hit)
        self.assertEqual(null_result.reason, REJECT_PARAMETER_SHAPE)

    def test_custom_tag_splits_plans(self):
        normal = ParameterSummary(values={1: 10}, tags={1: "selectivity:low"})
        extreme = ParameterSummary(values={1: 10}, tags={1: "selectivity:high"})
        self.cache.put(self.query, "normal_plan", 1.0, normal)

        self.assertTrue(self.cache.lookup(self.query, normal).hit)
        extreme_result = self.cache.lookup(self.query, extreme)
        self.assertEqual(extreme_result.reason, REJECT_PARAMETER_SHAPE)


class CapacityTest(unittest.TestCase):
    def test_lru_eviction_reports_set(self):
        cache = PlanCache(SchemaCatalog(), capacity=2)
        first = amount_eq_literal(1)
        second = select_orders(BinaryOp(">", Column("amount", "o"), Literal(1)))
        third = select_orders(BinaryOp("<", Column("amount", "o"), Literal(1)))

        put_first = cache.put(first, "p1", 1.0)
        cache.put(second, "p2", 2.0)
        put_third = cache.put(third, "p3", 3.0)

        self.assertEqual(put_first.evicted_for_capacity, set())
        self.assertEqual(put_third.evicted_for_capacity, {put_first.key})
        self.assertEqual(len(cache), 2)

    def test_replaced_entry_reports_flag(self):
        cache = PlanCache(SchemaCatalog())
        query = amount_eq_literal(1)
        cache.put(query, "v1", 10.0)
        result = cache.put(query, "v2", 9.0)
        self.assertTrue(result.replaced)
        self.assertEqual(cache.lookup(query).plan, "v2")


if __name__ == "__main__":
    unittest.main()
