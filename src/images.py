#!/usr/bin/env python

import logging
import os
import shutil
import subprocess
import tempfile
import time
import yaml

import openstack
import os_client_config
import requests
import shade

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO, datefmt='%Y-%m-%d %H:%M:%S')

CLOUD = os.environ.get('CLOUD', 'images')
IMAGESFILE = os.environ.get('IMAGESFILE', 'etc/images.yml')
REQUIRED_KEYS = [
    'format',
    'name',
    'status',
    'versions',
    'visibility',
]

with open(IMAGESFILE) as fp:
    images = yaml.load(fp)

conn = openstack.connect(cloud=CLOUD)
cloud = shade.openstack_cloud(cloud=CLOUD)
glance = os_client_config.make_client("image", cloud=CLOUD)

def create_import_task(glance, image, url):
    logging.info("Creating import task '%s'" % name)

    input = {
        'import_from_format': image['format'],
        'import_from': url,
        'image_properties': {
            'container_format': 'bare',
            'disk_format': image['format'],
            'min_disk': image.get('min_disk', 0),
            'min_ram': image.get('min_ram', 0),
            'name': name,
            'visibility': 'private'
        }
    }

    t = glance.tasks.create(type='import', input=input)

    while True:
        try:
            status = glance.tasks.get(t.id).status
            if status not in ['failure', 'success']:
                logging.info("Waiting for task %s" % t.id)
                time.sleep(10.0)
            else:
                break

        except:
            time.sleep(5.0)
            pass

    return status

def get_images(conn):
    result = {}

    for image in conn.list_images():
        if image.is_public or image.owner == conn.current_project_id:
            result[image.name] = image

    return result

cloud_images = get_images(conn)

for image in images:
    skip = False
    for required_key in REQUIRED_KEYS:
        if required_key not in image:
            logging.error("'%s' lacks the necessary key %s" % (image['name'], required_key))
            skip = True
    if skip:
        continue

    logging.info("Processing '%s'" % image['name'])

    versions = sorted(image['versions'].keys())
    for version in versions:
        if image['multi']:
            name = "%s (%s)" % (image['name'], version)
        else:
            name = "%s %s" % (image['name'], version)

        logging.info("Processing image '%s'" % name)

        existence = name in cloud_images

        if image['multi'] and len(versions) > 1 and version == versions[-1] and not existence:
            previous = "%s (%s)" % (image['name'], versions[-2])
            existence = previous in cloud_images and image['name'] in cloud_images
        elif image['multi'] and len(versions) > 1 and version == versions[-2] and not existence:
            existence = image['name'] in cloud_images
        elif image['multi'] and len(versions) == 1:
            existence = image['name'] in cloud_images

        status = None
        if not existence:

            url = image['versions'][version]['url']

            r = requests.head(url)
            logging.info("Tested URL %s: %s" % (url, r.status_code))

            if r.status_code not in [200, 302]:
                logging.warning("Skipping '%s'" % name)
                continue

            status = create_import_task(glance, image, url)

            if status == 'success':
                cloud_images = get_images(conn)

        if image['multi'] and existence and version == versions[-1] and image['name'] in cloud_images:
            name = image['name']

        if name in cloud_images:
            logging.info("Checking parameters of '%s'" % name)

            cloud_image = cloud_images[name]
            properties = cloud_image.properties

            if 'min_disk' in image and image['min_disk'] != cloud_image.min_disk:
                logging.info("Setting min_disk: %s != %s" % (image['min_disk'], cloud_image.min_disk))
                cloud.update_image_properties(name_or_id=cloud_image.id, min_disk=image['min_disk'])

            if 'min_ram' in image and image['min_ram'] != cloud_image.min_ram:
                logging.info("Setting min_ram: %s != %s" % (image['min_ram'], cloud_image.min_ram))
                cloud.update_image_properties(name_or_id=cloud_image.id, min_ram=image['min_ram'])

            if not image['multi']:
                image['meta']['os_version'] = version

            for property in properties:
                if property in image['meta']:
                    if image['meta'][property] != properties[property]:
                        logging.info("Setting %s: %s != %s" % (property, properties[property], image['meta'][property]))
                        glance.images.update(cloud_image.id, **{property: str(image['meta'][property])})
                elif property not in ['self', 'schema']:
                    # FIXME: handle deletion of properties
                    logging.info("Deleting %s" % (property))

            for property in image['meta']:
                if property not in properties:
                    logging.info("Setting %s: %s" % (property, image['meta'][property]))
                    glance.images.update(cloud_image.id, **{property: str(image['meta'][property])})

            logging.info("Checking status of '%s'" % name)
            if cloud_image.status != image['status'] and image['status'] == 'deactivated':
                logging.info("Deactivating image '%s'" % name)
                glance.images.deactivate(cloud_image.id)
            elif cloud_image.status != image['status'] and image['status'] == 'active':
                logging.info("Reactivating image '%s'" % name)
                glance.images.reactivate(cloud_image.id)

            logging.info("Checking visibility of '%s'" % name)
            if cloud_image.visibility != image['visibility']:
                logging.info("Set visibility of '%s' to '%s'" % (name, image['visibility']))
                glance.images.update(cloud_image.id, visibility=image['visibility'])

    if image['multi'] and len(versions) > 1:
        name = image['name']
        latest = "%s (%s)" % (image['name'], versions[-1])
        current = "%s (%s)" % (image['name'], versions[-2])

        if name in cloud_images and current not in cloud_images:
            logging.info("Rename %s to %s" % (name, current))
            glance.images.update(cloud_images[name].id, name=current)

        if latest in cloud_images:
            logging.info("Rename %s to %s" % (latest, name))
            glance.images.update(cloud_images[latest].id, name=name)

        cloud_images = get_images(conn)
    elif image['multi'] and len(versions) == 1:
        name = image['name']
        latest = "%s (%s)" % (image['name'], versions[-1])

        if latest in cloud_images:
            logging.info("Rename %s to %s" % (latest, name))
            glance.images.update(cloud_images[latest].id, name=name)
