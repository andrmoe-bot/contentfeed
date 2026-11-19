import hashlib
import unittest
from unittest.mock import patch

import jellyfin
import server


class JellyfinImageRelayTests(unittest.TestCase):
    def test_post_uses_same_origin_preview_url_and_keeps_source_for_relay(self):
        server_url = "http://jellyfin.home.arpa:8096"
        item_id = "bc0498e3af0438a454a6f639c66bd0f9"
        item = {
            "Id": item_id,
            "Type": "Episode",
            "Name": "Pitter Patter",
            "SeriesName": "The Singing Detective",
            "IndexNumber": 5,
            "ParentIndexNumber": 1,
            "ImageTags": {"Primary": "image-tag"},
        }

        post = jellyfin.post(server_url, item)

        key = hashlib.sha256(server_url.encode()).hexdigest()[:12]
        self.assertEqual(post["image"], f"/jellyfin/{key}/images/{item_id}")
        self.assertEqual(post["_jellyfin_image"],
                         f"{server_url}/Items/{item_id}/Images/Primary?maxWidth=640&tag=image-tag")

    def test_image_target_resolves_only_known_posts_on_matching_server(self):
        server_url = "http://jellyfin.home.arpa:8096"
        item_id = "bc0498e3af0438a454a6f639c66bd0f9"
        post = {
            "url": f"{server_url}/web/#/details?id={item_id}&serverId=server",
            "jellyfin": True,
            "image": "/jellyfin/key/images/" + item_id,
            "_jellyfin_image": f"{server_url}/Items/{item_id}/Images/Primary?maxWidth=640&tag=image-tag",
        }
        posts = {"feed": {"items": [post]}}
        key = hashlib.sha256(server_url.encode()).hexdigest()[:12]
        with patch.object(server, "subscriptions", posts), \
                patch.object(server.jellyfin, "read_logins", return_value=({}, None)), \
                patch.object(server.jellyfin, "login_for", return_value={"username": "feed"}):
            result = server.jellyfin_image_target(key, item_id)
            wrong_server = server.jellyfin_image_target("0" * 12, item_id)

        self.assertEqual(result, ((server_url, {"username": "feed"}),
                                  f"/Items/{item_id}/Images/Primary?maxWidth=640&tag=image-tag"))
        self.assertIsNone(wrong_server)

    def test_cached_direct_image_is_migrated_to_same_origin_route(self):
        server_url = "http://jellyfin.home.arpa:8096"
        item_id = "bc0498e3af0438a454a6f639c66bd0f9"
        source = f"{server_url}/Items/{item_id}/Images/Primary?maxWidth=640&tag=old-tag"
        subscriptions = {"feed": {"items": [{
            "url": f"{server_url}/web/#/details?id={item_id}",
            "jellyfin": True,
            "image": source,
        }]}}
        with patch.object(server, "subscriptions", subscriptions):
            changed = server.migrate_jellyfin_images()

        post = subscriptions["feed"]["items"][0]
        key = hashlib.sha256(server_url.encode()).hexdigest()[:12]
        self.assertTrue(changed)
        self.assertEqual(post["image"], f"/jellyfin/{key}/images/{item_id}")
        self.assertEqual(post["_jellyfin_image"], source)


if __name__ == "__main__":
    unittest.main()
