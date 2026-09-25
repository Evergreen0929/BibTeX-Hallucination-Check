import unittest

import check_citation_v0_dev as checker


class StrictMatchingTest(unittest.TestCase):
    def test_latex_title_normalization(self):
        self.assertTrue(
            checker.title_matches(
                "{$\\pi^3$}: Permutation-Equivariant Visual Geometry Learning",
                "$π^3$: Permutation-Equivariant Visual Geometry Learning",
            )
        )

    def test_full_author_order(self):
        bib = "Hu, Edward J and Shen, Yelong and Wallis, Phillip"
        self.assertTrue(checker.authors_match(bib, ["Edward J Hu", "Yelong Shen", "Phillip Wallis"]))
        self.assertFalse(checker.authors_match(bib, ["Edward J Hu", "Phillip Wallis", "Yelong Shen"]))
        self.assertTrue(checker.authors_match(bib, ["Hu, Edward J", "Shen, Yelong", "Wallis, Phillip"]))

    def test_compound_family_name(self):
        bib = "Suris Coll-Vinent, Didac and Carion, Nicolas"
        self.assertTrue(checker.authors_match(bib, ["Didac Suris Coll-Vinent", "Nicolas Carion"]))
        self.assertFalse(checker.authors_match("Gool, LV", ["Luc Van Gool"]))

    def test_middle_initial_variants(self):
        self.assertTrue(checker.authors_match("Shou, Mike Z", ["Mike Shou"]))
        self.assertTrue(checker.authors_match("Shou, Mike", ["Mike Zheng Shou"]))

    def test_latex_family_name(self):
        bib = r"Nie{\ss}ner, Matthias and Dai, Angela"
        self.assertTrue(checker.authors_match(bib, ["Matthias Niessner", "Angela Dai"]))

    def test_latex_caron(self):
        bib = r"Raji{\v{c}}, Frano"
        self.assertTrue(checker.authors_match(bib, ["Frano Rajic"]))

    def test_others_requires_correct_prefix(self):
        bib = "Sun, Xiangyu and Jiang, Haoyi and others"
        self.assertTrue(checker.authors_match(bib, ["Xiangyu Sun", "Haoyi Jiang", "Liu Liu"]))
        self.assertFalse(checker.authors_match(bib, ["Xiangyu Sun", "Liu Liu", "Haoyi Jiang"]))

    def test_venue_aliases(self):
        self.assertTrue(
            checker.venue_matches(
                "2025 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)",
                "Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition",
            )
        )
        self.assertTrue(checker.venue_matches("WACV", "Winter Conference on Applications of Computer Vision"))

    def test_neurips_article_convention(self):
        entry = {"ENTRYTYPE": "article", "journal": "NeurIPS"}
        candidate = {"source": "Official page", "type": "proceedings-article", "venue": "NeurIPS"}
        self.assertTrue(checker.type_matches(entry, candidate))

    def test_final_venue_is_not_silently_reported_as_arxiv_only(self):
        entry = {
            "ENTRYTYPE": "article",
            "title": "A Paper",
            "author": "Doe, Jane",
            "year": "2024",
            "journal": "arXiv preprint arXiv:2401.00001",
            "url": "https://arxiv.org/abs/2401.00001",
        }
        arxiv = checker.evaluate(entry, {
            "source": "arXiv", "title": "A Paper", "authors": ["Jane Doe"],
            "year": "2024", "venue": "arXiv", "type": "preprint", "doi": "",
            "url": entry["url"], "arxiv_id": "2401.00001",
        })
        final = checker.evaluate(entry, {
            "source": "Crossref", "title": "A Paper", "authors": ["Jane Doe"],
            "year": "2024", "venue": "CVPR", "type": "proceedings-article", "doi": "10.1/x",
            "url": "https://doi.org/10.1/x",
        })
        passed, _, problems = checker.aggregate(entry, [arxiv, final])
        self.assertFalse(passed)
        self.assertIn("publication status", problems)

    def test_current_arxiv_metadata_must_match(self):
        entry = {
            "ENTRYTYPE": "article", "title": "Old Title", "author": "Doe, Jane",
            "year": "2024", "journal": "arXiv preprint arXiv:2401.00001",
        }
        old_page = checker.evaluate(entry, {
            "source": "Official page", "title": "Old Title", "authors": ["Doe, Jane"],
            "year": "2024", "venue": "arXiv", "type": "preprint", "doi": "",
            "url": "https://arxiv.org/abs/2401.00001v1", "arxiv_id": "2401.00001",
        })
        current = checker.evaluate(entry, {
            "source": "arXiv", "title": "New Title", "authors": ["Jane Doe"],
            "year": "2024", "venue": "arXiv", "type": "preprint", "doi": "",
            "url": "https://arxiv.org/abs/2401.00001v2", "arxiv_id": "2401.00001",
        })
        passed, _, problems = checker.aggregate(entry, [old_page, current])
        self.assertFalse(passed)
        self.assertIn("current arXiv metadata", problems)

    def test_fields_cannot_be_mixed_across_incompatible_candidates(self):
        entry = {
            "ENTRYTYPE": "article", "title": "A Paper", "author": "Doe, Jane",
            "year": "2024", "journal": "A Journal",
        }
        wrong_year = checker.evaluate(entry, {
            "source": "Crossref", "title": "A Paper", "authors": ["Jane Doe"],
            "year": "2023", "venue": "A Journal", "type": "journal-article", "doi": "", "url": "",
        })
        wrong_venue = checker.evaluate(entry, {
            "source": "OpenAlex", "title": "A Paper", "authors": ["Jane Doe"],
            "year": "2024", "venue": "Another Journal", "type": "journal-article", "doi": "", "url": "",
        })
        passed, _, problems = checker.aggregate(entry, [wrong_year, wrong_venue])
        self.assertFalse(passed)
        self.assertIn("single-source consistency", problems)

    def test_same_version_source_conflict_requires_review(self):
        entry = {
            "ENTRYTYPE": "article", "title": "A Paper", "author": "Doe, Jane",
            "year": "2024", "journal": "A Journal",
        }
        good = checker.evaluate(entry, {
            "source": "Official page", "title": "A Paper", "authors": ["Jane Doe"],
            "year": "2024", "venue": "A Journal", "type": "journal-article", "doi": "", "url": "",
        })
        conflicting = checker.evaluate(entry, {
            "source": "Crossref", "title": "A Paper", "authors": ["Jane Doe"],
            "year": "2023", "venue": "A Journal", "type": "journal-article", "doi": "", "url": "",
        })
        passed, _, problems = checker.aggregate(entry, [good, conflicting])
        self.assertFalse(passed)
        self.assertIn("source consistency", problems)

    def test_conflicting_aggregator_does_not_override_primary_match(self):
        entry = {
            "ENTRYTYPE": "article",
            "title": "Contrastive Lift: 3D Object Instance Segmentation by Slow-Fast Contrastive Fusion",
            "author": "Bhalgat, Yash and Laina, Iro and Henriques, Joao F and Zisserman, Andrew and Vedaldi, Andrea",
            "year": "2023",
            "journal": "arXiv preprint arXiv:2306.04633",
        }
        wrong_crossref = checker.evaluate(entry, {
            "source": "Crossref",
            "title": entry["title"],
            "authors": ["Yash Bhalgat", "Iro Laina", "Joao F Henriques", "Andrea Vedaldi", "Andrew Zisserman"],
            "year": "2023", "venue": "arXiv", "type": "posted-content", "doi": "", "url": "",
        })
        correct_arxiv = checker.evaluate(entry, {
            "source": "arXiv",
            "title": entry["title"],
            "authors": ["Yash Bhalgat", "Iro Laina", "Joao F Henriques", "Andrew Zisserman", "Andrea Vedaldi"],
            "year": "2023", "venue": "arXiv", "type": "preprint", "doi": "", "url": "http://arxiv.org/abs/2306.04633",
            "arxiv_id": "2306.04633",
        })
        passed, _, problems = checker.aggregate(entry, [wrong_crossref, correct_arxiv])
        self.assertTrue(passed, problems)


if __name__ == "__main__":
    unittest.main()
