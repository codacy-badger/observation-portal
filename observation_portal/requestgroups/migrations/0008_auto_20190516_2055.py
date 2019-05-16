# Generated by Django 2.1.7 on 2019-05-16 20:55

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requestgroups', '0007_auto_20190516_1713'),
    ]

    operations = [
        migrations.AlterField(
            model_name='guidingconfig',
            name='exposure_time',
            field=models.FloatField(blank=True, help_text='Guiding exposure time', null=True, validators=[django.core.validators.MinValueValidator(0.0), django.core.validators.MaxValueValidator(120.0)]),
        ),
    ]
