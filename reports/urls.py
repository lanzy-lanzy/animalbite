from django.urls import path
from . import views

app_name = 'reports'

urlpatterns = [
    path('', views.reports_index, name='reports_index'),
    path('cases/', views.cases_report, name='cases_report'),
    path('patient-treatment/', views.patient_treatment_report,
         name='patient_treatment_report'),
    path('patients/', views.patients_report, name='patients_report'),
    path('animal-bite-cases/', views.animal_bite_cases_report,
         name='animal_bite_cases_report'),
    path('vaccine/', views.vaccine_report, name='vaccine_report'),
    path('vaccines/', views.vaccine_inventory_report,
         name='vaccine_inventory_report'),
    path('medicines/', views.medicine_inventory_report,
         name='medicine_inventory_report'),
    path('stock-alerts/', views.stock_alerts_report,
         name='stock_alerts_report'),
    path('barangay/', views.barangay_report, name='barangay_report'),
    path('barangay/<str:barangay>/', views.barangay_detail_report,
         name='barangay_detail_report'),
    path('animal-type/', views.animal_type_report, name='animal_type_report'),
    path('animal-type/<str:animal_type>/', views.animal_type_detail_report,
         name='animal_type_detail_report'),
    path('missed/', views.missed_appointments_report, name='missed_appointments'),
    path('completed-vaccinations/', views.completed_vaccinations_report,
         name='completed_vaccinations'),
    path('under-treatment/', views.under_treatment_report,
         name='under_treatment_report'),
    path('vaccinated-by-dose/', views.vaccinated_by_dose_report,
         name='vaccinated_by_dose_report'),
]
