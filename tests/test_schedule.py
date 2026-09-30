import unittest

from mineru_queue.schedule import ServiceLoad, choose_service


def load(name, count, nbytes, target=6, maximum=8):
    return ServiceLoad(name, count, nbytes, target, maximum)


class ChooseServiceTest(unittest.TestCase):
    def test_fills_the_least_busy_service_up_to_its_target(self):
        services = [load("h100-b", 1, 300), load("h100-a", 0, 0), load("h100-c", 1, 50)]
        self.assertEqual(choose_service(services, 999), "h100-a")

    def test_after_target_assigns_only_when_the_new_file_fits_the_byte_gap(self):
        services = [load("a", 6, 100), load("b", 6, 200), load("c", 6, 300)]
        self.assertEqual(choose_service(services, 150), "a")
        self.assertIsNone(choose_service(services, 250))

    def test_service_at_max_is_left_out_of_the_byte_gap(self):
        services = [
            load("a100", 4, 1000, target=2, maximum=4),
            load("b", 6, 100),
            load("c", 6, 180),
        ]
        self.assertIsNone(choose_service(services, 200))
        self.assertEqual(choose_service(services, 50), "b")

    def test_per_service_target_is_filled_before_extra_tasks(self):
        services = [
            load("a100", 1, 10, target=2, maximum=4),
            load("h100", 6, 10, target=6, maximum=8),
        ]
        self.assertEqual(choose_service(services, 500), "a100")

    def test_single_service_below_max_keeps_accepting(self):
        services = [
            load("a100", 4, 10, target=2, maximum=4),
            load("h100", 6, 80, target=6, maximum=8),
        ]
        self.assertEqual(choose_service(services, 500), "h100")

    def test_equal_bytes_do_not_add_an_extra_task(self):
        services = [load("a", 6, 100), load("b", 6, 100)]
        self.assertIsNone(choose_service(services, 1))

    def test_extra_slots_can_move_to_whichever_service_is_lightest(self):
        services = [load("a", 6, 100), load("b", 6, 200), load("c", 6, 300)]
        self.assertEqual(choose_service(services, 150), "a")
        updated = [load("a", 7, 250), load("b", 6, 200), load("c", 6, 300)]
        self.assertEqual(choose_service(updated, 40), "b")

    def test_everyone_at_max_waits(self):
        services = [load("a", 8, 10), load("b", 4, 10, target=2, maximum=4)]
        self.assertIsNone(choose_service(services, 1))


if __name__ == "__main__":
    unittest.main()
