# Generated by Django 2.2.1 on 2019-06-20 22:25

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='terms_accepted',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
