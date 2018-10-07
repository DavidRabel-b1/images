#!/usr/bin/env python

import logging
import os
import shutil
import subprocess
import tempfile
import time
import yaml

import os_client_config
import requests
import shade

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO, datefmt='%Y-%m-%d %H:%M:%S')
shade.simple_logging(debug=os.environ.get('DEBUG', False))

CLOUD= os.environ.get('CLOUD', 'images')
IMAGESFILE = os.environ.get('IMAGESFILE', 'etc/images.yml')
REQUIRED_KEYS = [
    'name',
    'format',
    'status',
    'visibility'
]

with open(IMAGESFILE) as fp:
    images = yaml.load(fp)

cloud = shade.openstack_cloud(cloud=CLOUD)
glance = os_client_config.make_client("image", cloud=CLOUD)

def get_images(cloud):
    result = {}
    for image in cloud.list_images():
        if image.is_public or image.owner == cloud.current_project_id:
            result[image.name] = image

    return result

cloud_images = get_images(cloud)

for image in images:

    skip = False

    # check required keys

    for required_key in REQUIRED_KEYS:
        if required_key not in image:
            logging.info("'%s' lacks the necessary key %s" % (image['name'], required_key))
            skip = True
    if skip:
        continue

    logging.info("Processing '%s'" % image['name'])

    # check existence
    existence = image['name'] in cloud_images

    status = None
    if not existence:
        # check image url

        r = requests.head(image['url'])
        logging.info("Test URL %s: %s" % (image['url'], r.status_code))

        if r.status_code not in [200, 302]:
            logging.info("Skipping '%s'" % image['name'])
            continue

        logging.info("Creating import task '%s'" % image['name'])
        input = {
            'import_from_format': image['format'],
            'import_from': image['url'],
            'image_properties': {
                'container_format': 'bare',
                'disk_format': image['format'],
                'min_disk': image.get('min_disk', 0),
                'min_ram': image.get('min_ram', 0),
                'name': image['name'],
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

        if status == 'success':
            cloud_images = get_images(cloud)

    if existence or (not existence and status == 'success'):
        logging.info("Checking parameters of '%s'" % image['name'])

        cloud_image = cloud_images[image['name']]
        properties = cloud_image.properties

        if 'min_disk' in image and image['min_disk'] != cloud_image.min_disk:
            logging.info("Setting min_disk: %s != %s" % (image['min_disk'], cloud_image.min_disk))
            cloud.update_image_properties(name_or_id=cloud_image.id, min_disk=image['min_disk'])

        if 'min_ram' in image and image['min_ram'] != cloud_image.min_ram:
            logging.info("Setting min_ram: %s != %s" % (image['min_ram'], cloud_image.min_ram))
            cloud.update_image_properties(name_or_id=cloud_image.id, min_ram=image['min_ram'])

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

        logging.info("Checking status of '%s'" % image['name'])
        if cloud_image.status != image['status'] and image['status'] == 'deactivated':
            logging.info("Deactivating image '%s'" % image['name'])
            glance.images.deactivate(cloud_image.id)
        elif cloud_image.status != image['status'] and image['status'] == 'active':
            logging.info("Reactivating image '%s'" % image['name'])
            glance.images.reactivate(cloud_image.id)

        logging.info("Checking visibility of '%s'" % image['name'])
        if cloud_image.visibility != image['visibility']:
            logging.info("Set visibility of '%s' to '%s'" % (image['name'], image['visibility']))
            glance.images.update(cloud_image.id, visibility=image['visibility'])
